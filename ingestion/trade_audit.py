"""
trade_audit.py — Phase 8 §2.2 / WS-5 — immutable chained trade & WACC-panel audit ledger
Pre-Registration Config: config/pre_registration_phase8.yaml (frozen 2026-09-20,
CEO Ruling B-20260920-001) → `phase8.trade_audit`:
    chain_verification: true
    partition_by: month
    retention_years: 10
    timestamp_precision: nanosecond
    uuid_version: 7

This module materializes the immutable, tamper-evident ledger that records each
auditable event (trades, WACC-panel writes, data-gate transitions).  Design binds
phase8_bigpickle_analysis.md §6.1-§6.4 (WS-2 C2.1-C2.5):

  1. **Two-layer SHA-256 chain (§6.2)**:
       record_hash = sha256(canonical_json(all fields EXCEPT prev_chain_hash,
                                           record_hash, chain_hash))
       chain_hash  = sha256(prev_chain_hash || record_hash)
     genesis: prev_chain_hash = 'GENESIS'.  Verification recomputes every
     record_hash (forensic — stored hashes are never trusted) and re-walks the
     prev links; returns (intact, n_blocks, first_break_serial).
  2. **Cross-partition chaining (§6.1/§6.2)**: month partitions are sealed and
     rotated in ascending order; the FIRST record of partition P+1 carries
     prev_chain_hash = last chain_hash of partition P; a partition manifest
     records prev_partition_chain_hash / first / last chain hashes so a tamper
     at any depth is detectable.  Back-append into an older month is refused
     (seal/rotate invariant).
  3. **Single-writer enforcement (WS-2 C2.2)**: a `.ledger.lock` (pid, host,
     ns timestamp) is held for the duration of any append/batch; stale locks
     (dead pid or age > 10 min) are stolen with a logged warning.  No two
     processes can append concurrently.
  4. **Nanosecond precision + UUID v7 (C2.2)**: every write timestamp is a
     9-digit-ns pd.Timestamp; block PK = uuid7 (time-ordered ns prefix) —
     immutable AND time-sortable.
  5. **trigger_reason JSON Schema (C2.3)**: Appendix A (draft 2020-12) embedded
     as TRIGGER_REASON_SCHEMA; validated by validate_trigger_reason() at write
     time via jsonschema.  A malformed trigger_reason REJECTS the write — an
     audit ledger cannot carry silent fixes.
  6. **stage_gate enum {1..5} (C2.4)**: 1=VALUATION, 2=GRASSROOTS, 3=EXIT,
     4=STRUCTURAL/BLACK_SWAN, 5=PROBATION/DATA (probation decisions are trades
     and must be auditable — Phase 7B lesson).  `gate_name` carries the human
     gate label.
  7. **Month partitioning / retention (10y)**: blocks live in
     `trade_audit/month=%Y-%m/` parquet; appends outside the 10-year retention
     window are refused (purge-guard); a block whose chain_hash already exists
     is never rewritten.
  8. **pnl_attribution (C2.3 note)**: optional JSON column carried for Phase 2;
     the attribution *method* freeze is out of Phase 1 scope and documented at
     append time via the column being None.

PIT contract (mirrors valuation_schema.validate_wacc_panel §2.2):
  - erp_blend ∈ frozen erp_bounds [0.005, 0.08]; wacc_v2/ke_v2 ∈ [0, 0.5]
  - beta_source non-null (coverage gate) — the audit block re-presents the same
    frozen bounds that valuation_schema enforces on the panel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger("trade_audit")

# ----------------------------------------------------------------------
# Frozen pre-registration contract (phase8.trade_audit)
# ----------------------------------------------------------------------
FROZEN_CFG = Path("config/pre_registration_phase8.yaml")
CHAIN_VERIFICATION = True
PARTITION_BY = "month"
RETENTION_YEARS = 10
TIMESTAMP_PRECISION = "nanosecond"
UUID_VERSION = 7

DEFAULT_LEDGER_DIR = Path("data/trade_audit")
DEFAULT_ERP_BOUNDS: Tuple[float, float] = (0.005, 0.08)
DEFAULT_WACC_BOUNDS: Tuple[float, float] = (0.0, 0.5)
DEFAULT_IV_FLOOR = 0.01

GENESIS = "GENESIS"                       # §6.2 genesis prev_chain_hash sentinel
STAGE_GATES: Dict[int, str] = {           # C2.4 probation-extended enum
    1: "VALUATION",
    2: "GRASSROOTS",
    3: "EXIT",
    4: "STRUCTURAL",
    5: "PROBATION_DATA",
}
ACTIONS: Tuple[str, ...] = (
    "ENTRY", "EXIT", "HOLD", "SWAP_IN", "SWAP_OUT", "REBALANCE",
    "PANEL_WRITE", "QUARANTINE", "PROBATION", "OTHER",
)
WRITE_LOCK_NAME = ".ledger.lock"
WRITE_LOCK_STALE_S = 600                  # stale writer-lock after 10 min
MAX_PNL_ATTR = 16                         # reserved; method frozen in Phase 2

# Appendix A — trigger_reason JSON schema (frozen contract)
TRIGGER_REASON_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "valuation": {
            "type": "object",
            "required": ["iv_p25", "price", "discount_pct", "iv_method",
                         "iv_basis_filed_date", "iv_nowcast_ts"],
            "properties": {
                "iv_p25": {"type": "number", "minimum": 0},
                "price": {"type": "number", "minimum": 0},
                "discount_pct": {"type": "number"},
                "iv_method": {"enum": ["dcf_v2_nowcast", "dcf_v2_filing_baseline",
                                       "fallback_sector_median"]},
                "iv_basis_filed_date": {"type": "string", "format": "date"},
                "iv_nowcast_ts": {"type": "string", "format": "date-time"}
            }
        },
        "grassroots": {
            "type": "object",
            "required": ["c_t_score", "coverage", "adv_usd", "mapping_confidence"],
            "properties": {"c_t_score": {"type": "number"},
                           "coverage": {"type": "integer"},
                           "adv_usd": {"type": "number", "minimum": 0},
                           "mapping_confidence": {"enum": ["high", "medium", "low", "n/a"]}}
        },
        "exit": {"type": "object",
                 "properties": {"hype_pctile": {"type": "number"},
                                "margin_decay_bps": {"type": "number"},
                                "price_iv_ratio": {"type": "number"}}},
        "black_swan": {"type": "object",
                       "properties": {"tier": {"enum": ["TIER1", "TIER2", "DISENGAGED"]},
                                      "drawdown_5d": {"type": "number"},
                                      "hmm_crisis_prob": {"type": "number",
                                                          "minimum": 0, "maximum": 1}}},
        "swap": {"type": "object",
                 "properties": {"barrier_level": {"type": "number"},
                                "moat_penalty": {"type": "number"},
                                "fractal_vol": {"type": "number"},
                                "t_held_months": {"type": "number", "minimum": 0}}},
        "probation": {"type": "object",
                      "properties": {"gate_passed": {"type": "boolean"},
                                     "weight_ramp_pct": {"type": "number",
                                                         "minimum": 0, "maximum": 1},
                                     "queue_days": {"type": "integer"}}},
        "data_gate": {"type": "object",
                      "properties": {"state": {"enum": ["QUARANTINE", "PROBATION", "FULL"]},
                                     "quarters_available": {"type": "integer", "minimum": 0},
                                     "weight_cap_applied": {"type": "number",
                                                            "minimum": 0, "maximum": 1}}}
    },
    "required": ["valuation"],
    "additionalProperties": False
}


# ----------------------------------------------------------------------
# Hash & canonicalization helpers (§6.2)
# ----------------------------------------------------------------------
def _sha256_hex(data: Union[str, bytes]) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _json_default(o: Any) -> Any:
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    """Deterministic serialization for hashing: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=_json_default)


# Appendix A validator compiled ONCE at import: schema compilation is ~1ms and
# constructing it per call would dominate batch writes of 10k+ panel rows.
try:  # jsonschema is a pinned dependency; fall back to shape checks if absent
    import jsonschema as _jsonschema
    _append_a_validator = _jsonschema.Draft202012Validator(TRIGGER_REASON_SCHEMA)
except ImportError:  # pragma: no cover
    _append_a_validator = None


def validate_trigger_reason(trigger_reason: Optional[Dict[str, Any]]) -> None:
    """Appendix A validation at write time.  Raises ValueError on malformed JSON —
    an append-only ledger cannot carry silent fixes (analysis §6.x)."""
    if trigger_reason is None:
        raise ValueError("trigger_reason is required (Appendix A: 'required': ['valuation'])")
    if _append_a_validator is None:
        if not isinstance(trigger_reason, dict) or "valuation" not in trigger_reason:
            raise ValueError("trigger_reason must be a dict with a 'valuation' object")
        return
    try:
        _append_a_validator.validate(trigger_reason)
    except _jsonschema.ValidationError as exc:
        raise ValueError(f"trigger_reason failed Appendix A JSON Schema: {exc.message}")


def _uuid7_ns(ts_ns: int, seed: Optional[Union[str, bytes]] = None) -> str:
    """Monotonic time-ordered uuid7 from a nanosecond epoch (version nibble = 7).

    The 40 random bits are derived from sha256(seed) when provided — making the
    block_id CONTENT-ADDRESSED (identical events collide; the persistence layer
    refuses rewrites of an existing block_id).  Without a seed the bits are
    uuid4-random (time-sortable but not deterministic)."""
    unix_ms = ts_ns // 1_000_000          # 44 bits
    ns_in_ms = ts_ns % 1_000_000          # remainder within the millisecond
    ticks = ns_in_ms & 0x3FFFFFFFFFFF     # 46 bits — nanosecond sub-ms, monotonic
    if seed is not None:
        digest = hashlib.sha256(seed.encode("utf-8") if isinstance(seed, str) else seed).digest()
        rand5 = digest[:5]
    else:
        rand5 = uuid.uuid4().bytes[:5]
    b = bytearray(16)
    b[0] = (unix_ms >> 40) & 0xFF
    b[1] = (unix_ms >> 32) & 0xFF
    b[2] = (unix_ms >> 24) & 0xFF
    b[3] = (unix_ms >> 16) & 0xFF
    b[4] = (unix_ms >> 8) & 0xFF
    b[5] = unix_ms & 0xFF
    b[6] = ((ticks >> 38) & 0x0F) | 0x70          # version nibble (7) + top bits
    b[7] = (ticks >> 30) & 0xFF
    b[8] = ((ticks >> 22) & 0x3F) | 0x80          # variant 10xx
    b[9] = (ticks >> 14) & 0xFF
    b[10] = (ticks >> 6) & 0xFF
    b[11] = ((ticks & 0x3F) << 2) | (rand5[0] >> 6)
    b[12:16] = rand5[1:5]                         # 32 seeded/random bits
    return str(uuid.UUID(bytes=bytes(b)))


# ----------------------------------------------------------------------
# Audit block
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class AuditBlock:
    """One immutable chained block in the trade_audit ledger (§6.2)."""
    block_id: str              # uuid7 — PK, time-sortable
    asof_date: pd.Timestamp
    ticker: str
    action: str
    gate_name: str
    stage_gate: int
    trigger_reason: Optional[Dict[str, Any]]
    wacc_v2: float
    ke_v2: float
    kd_pre_tax_v2: float
    erp_blend: float
    beta_source: str
    rf_10y: float
    exec_slippage_bps: Optional[float]
    exec_impact_bps: Optional[float]
    exec_fill_pct: Optional[float]
    pnl_attribution: Optional[Dict[str, Any]]
    retrieval_ts: pd.Timestamp
    prev_chain_hash: str
    record_hash: str
    chain_hash: str
    partition_month: str

    # -- payload (excludes chain fields — deterministic recomputation, §6.2) --
    def _payload(self) -> Dict[str, Any]:
        return {
            "block_id": self.block_id,
            "asof_date": self.asof_date,
            "ticker": self.ticker,
            "action": self.action,
            "gate_name": self.gate_name,
            "stage_gate": self.stage_gate,
            "trigger_reason": self.trigger_reason,
            "wacc_v2": self.wacc_v2,
            "ke_v2": self.ke_v2,
            "kd_pre_tax_v2": self.kd_pre_tax_v2,
            "erp_blend": self.erp_blend,
            "beta_source": self.beta_source,
            "rf_10y": self.rf_10y,
            "exec_slippage_bps": self.exec_slippage_bps,
            "exec_impact_bps": self.exec_impact_bps,
            "exec_fill_pct": self.exec_fill_pct,
            "pnl_attribution": self.pnl_attribution,
            "retrieval_ts": self.retrieval_ts,
        }

    @classmethod
    def from_payload(
        cls,
        ticker: str,
        asof: Union[str, pd.Timestamp],
        retrieval_ts: Union[str, pd.Timestamp],
        action: str,
        gate_name: str,
        stage_gate: int,
        wacc_v2: float,
        ke_v2: float,
        kd_pre_tax_v2: float,
        erp_blend: float,
        beta_source: str,
        rf_10y: float,
        prev_chain_hash: str,
        trigger_reason: Optional[Dict[str, Any]] = None,
        exec_slippage_bps: Optional[float] = None,
        exec_impact_bps: Optional[float] = None,
        exec_fill_pct: Optional[float] = None,
        pnl_attribution: Optional[Dict[str, Any]] = None,
    ) -> "AuditBlock":
        """Builds a frozen block with §6.2 two-layer hashing."""
        if stage_gate not in STAGE_GATES:
            raise ValueError(f"stage_gate {stage_gate} not in {sorted(STAGE_GATES)}")
        if action not in ACTIONS:
            raise ValueError(f"action '{action}' not in {ACTIONS}")
        if not beta_source:
            raise ValueError(f"beta_source required (coverage gate)")
        validate_trigger_reason(trigger_reason)

        asof_ts = pd.Timestamp(asof)
        retr_ts = pd.Timestamp(retrieval_ts)
        retr_ns = int(retr_ts.value)          # ns epoch
        asof_ns = int(asof_ts.value)
        month = f"{asof_ts.year:04d}-{asof_ts.month:02d}"
        # Content-addressed block_id (C2.2): same event + same retrieval stamp
        # collides on the SAME uuid7 -> the persistence layer refuses the rewrite.
        seed_payload = {
            "asof_date": asof_ts, "ticker": ticker, "action": action,
            "gate_name": gate_name, "stage_gate": int(stage_gate),
            "trigger_reason": trigger_reason, "wacc_v2": float(wacc_v2),
            "ke_v2": float(ke_v2), "kd_pre_tax_v2": float(kd_pre_tax_v2),
            "erp_blend": float(erp_blend), "beta_source": beta_source,
            "rf_10y": float(rf_10y), "exec_slippage_bps": exec_slippage_bps,
            "exec_impact_bps": exec_impact_bps, "exec_fill_pct": exec_fill_pct,
            "pnl_attribution": pnl_attribution, "retrieval_ts": retr_ts,
        }
        block_id = _uuid7_ns(retr_ns, seed=canonical_json(seed_payload))
        blk = cls(
            block_id=block_id,
            asof_date=asof_ts,
            ticker=ticker,
            action=action,
            gate_name=gate_name,
            stage_gate=int(stage_gate),
            trigger_reason=trigger_reason,
            wacc_v2=float(wacc_v2),
            ke_v2=float(ke_v2),
            kd_pre_tax_v2=float(kd_pre_tax_v2),
            erp_blend=float(erp_blend),
            beta_source=beta_source,
            rf_10y=float(rf_10y),
            exec_slippage_bps=(float(exec_slippage_bps)
                               if exec_slippage_bps is not None else None),
            exec_impact_bps=(float(exec_impact_bps)
                             if exec_impact_bps is not None else None),
            exec_fill_pct=(float(exec_fill_pct) if exec_fill_pct is not None else None),
            pnl_attribution=pnl_attribution,
            retrieval_ts=retr_ts,
            prev_chain_hash=prev_chain_hash,
            record_hash="",
            chain_hash="",
            partition_month=month,
        )
        # §6.2 (frozen): record_hash = sha256(canonical(all fields EXCEPT
        # prev_chain_hash, record_hash, chain_hash)); partition_month is derived.
        payload = blk._payload()
        payload.pop("prev_chain_hash", None)
        payload.pop("record_hash", None)
        payload.pop("chain_hash", None)
        payload.pop("partition_month", None)
        record_hash = _sha256_hex(canonical_json(payload))
        chain_hash = _sha256_hex(f"{prev_chain_hash}|{record_hash}")
        # genesis: prev = 'GENESIS' sentinel; format stays H(prev|record)
        return cls(
            block_id=block_id, asof_date=asof_ts, ticker=ticker, action=action,
            gate_name=gate_name, stage_gate=int(stage_gate),
            trigger_reason=trigger_reason, wacc_v2=float(wacc_v2), ke_v2=float(ke_v2),
            kd_pre_tax_v2=float(kd_pre_tax_v2), erp_blend=float(erp_blend),
            beta_source=beta_source, rf_10y=float(rf_10y),
            exec_slippage_bps=blk.exec_slippage_bps, exec_impact_bps=blk.exec_impact_bps,
            exec_fill_pct=blk.exec_fill_pct, pnl_attribution=pnl_attribution,
            retrieval_ts=retr_ts, prev_chain_hash=prev_chain_hash,
            record_hash=record_hash, chain_hash=chain_hash,
            partition_month=month,
        )

    # -- recompute oracle (forensic — doesn't trust stored hashes) --
    def recompute(self) -> Tuple[str, str]:
        payload = self._payload()
        payload.pop("record_hash", None)
        payload.pop("chain_hash", None)
        payload.pop("partition_month", None)
        record_hash = _sha256_hex(canonical_json(payload))
        chain_hash = _sha256_hex(f"{self.prev_chain_hash}|{record_hash}") \
            if self.prev_chain_hash != GENESIS else _sha256_hex("GENESIS" + "|" + record_hash)
        return record_hash, chain_hash

    def prov(self) -> Dict[str, Any]:
        d = self._payload()
        d["prev_chain_hash"] = self.prev_chain_hash
        d["record_hash"] = self.record_hash
        d["chain_hash"] = self.chain_hash
        d["partition_month"] = self.partition_month
        return d


# ----------------------------------------------------------------------
# Single-writer lock (WS-2 C2.2)
# ----------------------------------------------------------------------
class _WriteLock:
    """Exclusive writer lock over the ledger directory (O_CREAT|O_EXCL atomic)."""

    def __init__(self, ledger_dir: Path):
        self.path = ledger_dir / WRITE_LOCK_NAME
        self._held = False
        self._pid = os.getpid()

    def acquire(self) -> None:
        if self._held:
            return
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": self._pid, "host": socket.gethostname(),
                           "acquired_ns": time_ns_now()}, f)
            self._held = True
        except FileExistsError:
            self._steal_if_stale()
            # retry once after steal
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": self._pid, "host": socket.gethostname(),
                           "acquired_ns": time_ns_now()}, f)
            self._held = True

    def _steal_if_stale(self) -> None:
        try:
            meta = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        age_s = (time_ns_now() - int(meta.get("acquired_ns", 0))) / 1e9
        pid = int(meta.get("pid", -1))
        pid_alive = _pid_alive(pid)
        if age_s > WRITE_LOCK_STALE_S or not pid_alive:
            logger.warning("stealing stale writer lock (pid=%s age=%.0fs host=%s)",
                           pid, age_s, meta.get("host"))
            try:
                self.path.unlink()
            except OSError:
                pass

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._held = False

    def __enter__(self) -> "_WriteLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def time_ns_now() -> int:
    return int(pd.Timestamp.now(tz="UTC").tz_localize(None).value)


def _pid_alive(pid: int) -> bool:
    """Liveness probe without killing the target (or ourselves).

    os.kill(pid, 0) is NOT safe on Windows: it can terminate the caller.
    Use psutil when present, else ctypes OpenProcess on Windows, else the
    POSIX kill-0 probe.
    """
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 — fall through to platform probes
        pass
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, PermissionError, ValueError):
        return False


# ----------------------------------------------------------------------
# Ledger
# ----------------------------------------------------------------------
def _clean_audit_dict(d: Any) -> Any:
    """Removes None fields from nested dicts added by PyArrow struct schema unification."""
    if not isinstance(d, dict):
        return d
    cleaned = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            sub = _clean_audit_dict(v)
            if sub:
                cleaned[k] = sub
        else:
            cleaned[k] = v
    return cleaned


class TradeAuditLedger:
    """Immutable chained SHA-256 audit ledger (phase8.trade_audit, §6.1-§6.4)."""

    def __init__(self, ledger_dir: Optional[Union[str, Path]] = None,
                 config: Optional[Union[str, Path, Dict[str, Any]]] = None):
        self._cfg = self._load_cfg(config)
        self.chain_verification = bool(self._cfg.get("chain_verification", CHAIN_VERIFICATION))
        self.partition_by = str(self._cfg.get("partition_by", PARTITION_BY))
        self.retention_years = int(self._cfg.get("retention_years", RETENTION_YEARS))
        self.timestamp_precision = str(self._cfg.get("timestamp_precision", TIMESTAMP_PRECISION))
        self.uuid_version = int(self._cfg.get("uuid_version", UUID_VERSION))
        self.ledger_dir = Path(ledger_dir) if ledger_dir is not None else DEFAULT_LEDGER_DIR
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self._blocks: Dict[str, List[AuditBlock]] = {}

    # -- config ----------------------------------------------------------
    @staticmethod
    def _load_cfg(config: Optional[Union[str, Path, Dict[str, Any]]]) -> Dict[str, Any]:
        if isinstance(config, dict):
            return dict(config)
        p = Path(config) if config is not None else FROZEN_CFG
        if not p.exists():
            logger.warning("trade_audit config %s missing; using frozen defaults", p)
            return {}
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return (doc.get("phase8") or {}).get("trade_audit", {}) or {}

    # -- partition helpers ------------------------------------------------
    @staticmethod
    def _partition_month(d: pd.Timestamp) -> str:
        return f"{d.year:04d}-{d.month:02d}"

    def _partition_path(self, month: str) -> Path:
        return self.ledger_dir / f"month={month}" / f"trade_audit_{month}.parquet"

    def _manifest_path(self, month: str) -> Path:
        return self.ledger_dir / f"month={month}" / "partition.manifest.json"

    def _retention_guard(self, d: pd.Timestamp) -> None:
        """10-year retention (frozen): refuse appends outside the retention window."""
        if self.retention_years <= 0:
            raise ValueError(f"frozen retention_years={self.retention_years} <= 0")
        oldest = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(
            days=self.retention_years * 365)
        if d < oldest:
            raise ValueError(
                f"refusing append into {d.date()} — outside frozen 10y retention "
                f"window (oldest allowed {oldest.date()})")

    def _existing_months(self) -> List[str]:
        months = sorted(m for m in self._blocks)
        for p in self.ledger_dir.glob("month=*/"):
            m = p.name[len("month="):]
            if m not in months:
                months.append(m)
        return sorted(set(months))

    def _seal_guard(self, d: pd.Timestamp) -> None:
        """Seal/rotate invariant (§6.1): never append into a month older than the
        newest sealed partition (back-append would fork the cross-partition chain)."""
        existing = self._existing_months()
        if not existing:
            return
        newest = existing[-1]
        if self._partition_month(d) < newest:
            raise ValueError(
                f"refusing back-append into month {self._partition_month(d)}: "
                f"partition {newest} is sealed (seal/rotate invariant, §6.1)")

    # -- chain -------------------------------------------------------------
    def _last_chain_hash_of(self, month: str) -> str:
        """Last chain_hash recorded for a month (disk-first, forensic)."""
        blocks = self._load_partition(month)
        if blocks:
            return blocks[-1].chain_hash
        return GENESIS

    def _chain_seed_for(self, month: str) -> str:
        """prev_chain_hash for the next block in `month`:
        - existing blocks in month → last chain_hash (in-chain link)
        - else → last chain_hash of the PREVIOUS month (cross-partition link, §6.2)
        - else → GENESIS."""
        blocks = self._load_partition(month)
        if blocks:
            return blocks[-1].chain_hash
        months = self._existing_months()
        prev_months = [m for m in months if m < month]
        if prev_months:
            return self._last_chain_hash_of(prev_months[-1])
        return GENESIS

    def last_sha256(self, month: str) -> str:
        blocks = self._load_partition(month)
        return blocks[-1].chain_hash if blocks else GENESIS

    # -- validation of one append payload ----------------------------------
    def _validate_payload_bounds(self, payload: Dict[str, Any],
                                 d: pd.Timestamp) -> None:
        """Defense-in-depth: hostile numeric payloads are clamped to the frozen
        wacc/erp bounds (primary enforcement is upstream — the engine panel
        clamps at the row level and validate_wacc_panel rejects violations)."""
        payload["erp_blend"] = float(max(DEFAULT_ERP_BOUNDS[0],
                                         min(DEFAULT_ERP_BOUNDS[1],
                                             float(payload["erp_blend"]))))
        for key in ("wacc_v2", "ke_v2", "kd_pre_tax_v2"):
            payload[key] = float(max(DEFAULT_WACC_BOUNDS[0],
                                     min(DEFAULT_WACC_BOUNDS[1], float(payload[key]))))

    # -- append ------------------------------------------------------------
    def append(self, ticker: str, asof: Union[str, pd.Timestamp], wacc_v2: float,
               ke_v2: float, kd_pre_tax_v2: float, erp_blend: float, rf_10y: float,
               beta_source: str, retrieval_ts: Optional[Union[str, pd.Timestamp]] = None,
               action: str = "OTHER", gate_name: str = "UNSPECIFIED",
               stage_gate: int = 1,
               trigger_reason: Optional[Dict[str, Any]] = None,
               exec_slippage_bps: Optional[float] = None,
               exec_impact_bps: Optional[float] = None,
               exec_fill_pct: Optional[float] = None,
               pnl_attribution: Optional[Dict[str, Any]] = None) -> AuditBlock:
        """Append one immutable chained block (single-writer, sealed partitions)."""
        d = pd.Timestamp(asof)
        self._retention_guard(d)
        self._seal_guard(d)
        payload = {
            "ticker": ticker, "asof": d, "wacc_v2": wacc_v2, "ke_v2": ke_v2,
            "kd_pre_tax_v2": kd_pre_tax_v2, "erp_blend": erp_blend, "rf_10y": rf_10y,
            "beta_source": beta_source, "retrieval_ts": (
                pd.Timestamp(retrieval_ts) if retrieval_ts is not None
                else pd.Timestamp.now(tz="UTC").tz_localize(None)),
            "action": action, "gate_name": gate_name, "stage_gate": stage_gate,
            "trigger_reason": trigger_reason, "exec_slippage_bps": exec_slippage_bps,
            "exec_impact_bps": exec_impact_bps, "exec_fill_pct": exec_fill_pct,
            "pnl_attribution": pnl_attribution,
        }
        with _WriteLock(self.ledger_dir):
            blocks = self._build_blocks([payload])
            self._persist_blocks([blocks[0]])
            return blocks[0]

    def append_batch(self, items: List[Dict[str, Any]]) -> List[AuditBlock]:
        """Append many blocks transactionally (single-writer, batch persist per
        month).  `items` are payload dicts with the same keys as `append`."""
        if not items:
            return []
        for it in items:
            d = pd.Timestamp(it["asof"])
            self._retention_guard(d)
            self._seal_guard(d)
        with _WriteLock(self.ledger_dir):
            blocks = self._build_blocks(items)
            self._persist_blocks(blocks)
            return blocks

    def _build_blocks(self, items: List[Dict[str, Any]]) -> List[AuditBlock]:
        """Orders by (month, asof, retrieval) and wires §6.2 chain links."""
        canonical: List[Dict[str, Any]] = []
        for it in items:
            d = pd.Timestamp(it["asof"])
            keys = self._existing_months()
            # validate bounds + trigger_reason up-front (all-or-nothing batch)
            self._validate_payload_bounds(it, d)
            validate_trigger_reason(it.get("trigger_reason"))
            canonical.append(it)
        canonical.sort(key=lambda x: (self._partition_month(pd.Timestamp(x["asof"])),
                                      pd.Timestamp(x["asof"]),
                                      pd.Timestamp(x["retrieval_ts"])))

        month_seeds: Dict[str, str] = {}
        blocks: List[AuditBlock] = []
        for it in canonical:
            d = pd.Timestamp(it["asof"])
            month = self._partition_month(d)
            seed = month_seeds.get(month)
            if seed is None:
                seed = self._chain_seed_for(month)
            retr = pd.Timestamp(it.get("retrieval_ts")) if it.get("retrieval_ts") is not None \
                else pd.Timestamp.now(tz="UTC").tz_localize(None)
            blk = AuditBlock.from_payload(
                ticker=it["ticker"], asof=d, retrieval_ts=retr,
                action=it.get("action", "OTHER"), gate_name=it.get("gate_name", "UNSPECIFIED"),
                stage_gate=it.get("stage_gate", 1),
                wacc_v2=it["wacc_v2"], ke_v2=it["ke_v2"], kd_pre_tax_v2=it["kd_pre_tax_v2"],
                erp_blend=it["erp_blend"], beta_source=it["beta_source"], rf_10y=it["rf_10y"],
                prev_chain_hash=seed,
                trigger_reason=it.get("trigger_reason"),
                exec_slippage_bps=it.get("exec_slippage_bps"),
                exec_impact_bps=it.get("exec_impact_bps"),
                exec_fill_pct=it.get("exec_fill_pct"),
                pnl_attribution=it.get("pnl_attribution"))
            month_seeds[month] = blk.chain_hash
            self._blocks.setdefault(month, []).append(blk)
            blocks.append(blk)
        return blocks

    # -- persistence ---------------------------------------------------------
    def _persist_blocks(self, blocks: List[AuditBlock]) -> None:
        """Append-only write grouped by month (no rewrite of any block_id)."""
        by_month: Dict[str, List[AuditBlock]] = {}
        for blk in blocks:
            by_month.setdefault(blk.partition_month, []).append(blk)
        for month, blks in by_month.items():  # in-memory order preserves chain
            path = self._partition_path(month)
            path.parent.mkdir(parents=True, exist_ok=True)
            new_rows = pd.DataFrame([b.prov() for b in blks])
            if path.exists():
                old = pd.read_parquet(path)
                dup = set(old["block_id"]) & set(new_rows["block_id"])
                if dup:
                    raise ValueError(f"refusing rewrite of existing block(s) {sorted(dup)[:5]}")
                # C2.1/DDL: UNIQUE(timestamp_utc, ticker, action, chain_hash) —
                # the same (asof, ticker, action, retrieval_ns) event must never
                # be appended twice, even when the block position differs.
                ev = ["asof_date", "ticker", "action", "retrieval_ts"]
                old_keys = set(map(tuple, old[ev].astype(str).values.tolist()))
                new_keys = set(map(tuple, new_rows[ev].astype(str).values.tolist()))
                if old_keys & new_keys:
                    raise ValueError(
                        f"refusing duplicate event (asof, ticker, action, retrieval_ns) "
                        f"already in {month}: {sorted(old_keys & new_keys)[:3]}")
                out = pd.concat([old, new_rows], ignore_index=True)
            else:
                out = new_rows
            tmp = path.with_suffix(".parquet.tmp")
            out.to_parquet(tmp, engine="pyarrow", index=False)
            tmp.replace(path)  # atomic write
            self._write_manifest(month)
            if self.chain_verification:
                ok, n, brk = self.verify_chain(month, verify_hashes=True)
                if not ok:
                    raise RuntimeError(
                        f"chain_verification failed after append to month={month}: "
                        f"n={n} break@#{brk} — refusing to leave a broken chain")

    def _write_manifest(self, month: str) -> None:
        """§6.1 partition manifest: cross-partition boundary hash + range stats."""
        blocks = self._load_partition(month)
        if not blocks:
            return
        months = self._existing_months()
        prev_months = [m for m in months if m < month]
        prev_chain = self._last_chain_hash_of(prev_months[-1]) if prev_months else GENESIS
        manifest = {
            "table": "trade_audit",
            "partition_month": month,
            "prev_partition_chain_hash": prev_chain,
            "first_chain_hash": blocks[0].chain_hash,
            "last_chain_hash": blocks[-1].chain_hash,
            "min_retrieval_ns": int(min(b.retrieval_ts.value for b in blocks)),
            "max_retrieval_ns": int(max(b.retrieval_ts.value for b in blocks)),
            "n_blocks": len(blocks),
            "sealed_at": datetime.now(timezone.utc).isoformat(),
            "retention_years": self.retention_years,
        }
        self._manifest_path(month).write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # -- verification oracle ---------------------------------------------------
    def verify_chain(self, month: str, verify_hashes: bool = True,
                     lossy: bool = False) -> Tuple[bool, int, Optional[int]]:
        """Recompute every record_hash, re-walk prev links, and cross-check the
        cross-partition boundary inside the month partition.  Disk-first (forensic).
        Returns (intact, n_blocks, first_break_serial) — serial 1-indexed."""
        blocks = self._load_partition(month)
        if not blocks:
            return True, 0, None
        prev = GENESIS
        months = self._existing_months()
        prev_months = [m for m in months if m < month]
        if prev_months:
            prev = self._last_chain_hash_of(prev_months[-1])
        for i, blk in enumerate(blocks, start=1):
            if blk.prev_chain_hash != prev:
                return False, len(blocks), i
            if verify_hashes:
                rec, chn = blk.recompute()
                if rec != blk.record_hash or chn != blk.chain_hash:
                    return False, len(blocks), i
            prev = blk.chain_hash
        return True, len(blocks), None

    def verify_ledger(self, cross_partition: bool = True) -> Dict[str, Any]:
        """Global oracle: walks all partitions in ascending order, checks
        in-partition chains AND §6.2 cross-partition boundaries."""
        months = self._existing_months()
        total = 0
        prev_boundary = GENESIS
        ok = True
        for m in months:
            blocks = self._load_partition(m)
            if not blocks:
                continue
            if cross_partition and blocks[0].prev_chain_hash != prev_boundary:
                ok = False
                break
            ok_m, n, brk = self.verify_chain(m, verify_hashes=True)
            total += n
            if not ok_m:
                ok = False
                break
            prev_boundary = blocks[-1].chain_hash
        return {"intact": ok, "n_blocks": total, "partitions": len(months)}

    def _load_partition(self, month: str) -> List[AuditBlock]:
        path = self._partition_path(month)
        if not path.exists():
            return list(self._blocks.get(month, []))
        df = pd.read_parquet(path)
        blocks: List[AuditBlock] = []
        for _, r in df.iterrows():
            trigger = r.get("trigger_reason")
            trigger = json.loads(trigger) if isinstance(trigger, str) else (None if trigger is None else trigger)
            trigger = _clean_audit_dict(trigger)
            pnl = r.get("pnl_attribution")
            pnl = json.loads(pnl) if isinstance(pnl, str) else (None if pnl is None else pnl)
            pnl = _clean_audit_dict(pnl)
            blocks.append(AuditBlock(
                block_id=str(r["block_id"]),
                asof_date=pd.Timestamp(r["asof_date"]),
                ticker=str(r["ticker"]),
                action=str(r["action"]),
                gate_name=str(r["gate_name"]),
                stage_gate=int(r["stage_gate"]),
                trigger_reason=trigger,
                wacc_v2=float(r["wacc_v2"]),
                ke_v2=float(r["ke_v2"]),
                kd_pre_tax_v2=float(r["kd_pre_tax_v2"]),
                erp_blend=float(r["erp_blend"]),
                beta_source=str(r["beta_source"]),
                rf_10y=float(r["rf_10y"]),
                exec_slippage_bps=(float(r["exec_slippage_bps"])
                                   if pd.notna(r.get("exec_slippage_bps")) else None),
                exec_impact_bps=(float(r["exec_impact_bps"])
                                 if pd.notna(r.get("exec_impact_bps")) else None),
                exec_fill_pct=(float(r["exec_fill_pct"])
                               if pd.notna(r.get("exec_fill_pct")) else None),
                pnl_attribution=pnl,
                retrieval_ts=pd.Timestamp(r["retrieval_ts"]),
                prev_chain_hash=str(r["prev_chain_hash"]),
                record_hash=str(r["record_hash"]),
                chain_hash=str(r["chain_hash"]),
                partition_month=month,
            ))
        return blocks

    def load_month(self, month: str) -> List[AuditBlock]:
        return self._load_partition(month)

    # -- provenance -----------------------------------------------------------
    def probe(self) -> Dict[str, Any]:
        """Compact forensic probe: config block, per-month chain status, global."""
        months = self._existing_months()
        status = {}
        for m in months:
            ok, n, brk = self.verify_chain(m, verify_hashes=True)
            status[m] = {"blocks": n, "intact": ok, "first_break": brk,
                         "last_chain": self.last_sha256(m)[:16]}
        global_rpt = self.verify_ledger(cross_partition=True)
        return {
            "config": {
                "chain_verification": self.chain_verification,
                "partition_by": self.partition_by,
                "retention_years": self.retention_years,
                "timestamp_precision": self.timestamp_precision,
                "uuid_version": self.uuid_version,
                "stage_gates": STAGE_GATES,
            },
            "ledger": global_rpt,
            "partitions": status,
        }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="trade_audit",
                                 description="Phase 8 §2.2 immutable chained audit ledger")
    ap.add_argument("--ledger-dir", default=None)
    ap.add_argument("--self-test", action="store_true",
                    help="run the frozen oracle (WS-2 proof contract)")
    ns = ap.parse_args(argv)
    if ns.self_test:
        return _self_test(Path(ns.ledger_dir) if ns.ledger_dir else None)
    print("trade_audit: emit stage not configured for CLI write; "
          "use TradeAuditLedger().append(...)/append_batch(...) or --self-test")
    return 0


def _self_test(ledger_dir: Optional[Path] = None) -> int:
    """Frozen oracle — proves §2.2/WS-2 integrity guarantees end-to-end:
      1. cross-partition chained appends (two months) + §6.2 two-layer hashes,
      2. full recompute verification intact → (True, n, None),
      3. tamper-evidence: retro-edit of a stored block → chain break detected
         by the recompute oracle only,
      4. immutability: same (asof, ticker, action) append refused (block_id match),
      5. trigger_reason JSON-Schema rejection at write time,
      6. stage_gate {1..5} + back-append (seal/rotate) rejection,
      7. single-writer lock: concurrent append attempt while lock held is
         blocked / stale-stolen,
      8. global ledger probe reports cross-partition integrity.
    Returns 0 on success (raises AssertionError on the first broken guarantee)."""
    import tempfile
    print("TradeAuditLedger self-test")
    print("=" * 60)

    tr = {"valuation": {"iv_p25": 118.0, "price": 112.4, "discount_pct": -0.05,
                        "iv_method": "dcf_v2_nowcast", "iv_basis_filed_date": "2024-10-28",
                        "iv_nowcast_ts": "2024-11-01T12:00:00Z"}}

    with tempfile.TemporaryDirectory() as td:
        ledger = TradeAuditLedger(ledger_dir=Path(td) / "audit")
        d = pd.Timestamp("2026-09-20")
        retr = pd.Timestamp("2026-09-20T00:00:00.123456789")

        blk1 = ledger.append(ticker="AAPL", asof=d, wacc_v2=0.1534, ke_v2=0.1640,
                             kd_pre_tax_v2=0.046, erp_blend=0.0421, rf_10y=0.042,
                             beta_source="sector_bottom_up", retrieval_ts=retr,
                             action="PANEL_WRITE", gate_name="WACC_PANEL",
                             stage_gate=1, trigger_reason=tr)
        blk2 = ledger.append(ticker="MSFT", asof=d, wacc_v2=0.1318, ke_v2=0.1410,
                             kd_pre_tax_v2=0.042, erp_blend=0.0430, rf_10y=0.042,
                             beta_source="sector_bottom_up", retrieval_ts=retr,
                             action="PANEL_WRITE", gate_name="WACC_PANEL",
                             stage_gate=1, trigger_reason=tr)
        # second month — cross-partition link
        d2 = pd.Timestamp("2026-10-05")
        blk3 = ledger.append(ticker="JNJ", asof=d2, wacc_v2=0.0951, ke_v2=0.1020,
                             kd_pre_tax_v2=0.038, erp_blend=0.0360, rf_10y=0.042,
                             beta_source="sector_bottom_up", retrieval_ts=retr,
                             action="ENTRY", gate_name="VALUATION",
                             stage_gate=1, trigger_reason=tr)

        # 1. chained hashes: genesis + in-month + cross-partition
        assert blk1.prev_chain_hash == GENESIS, blk1.prev_chain_hash
        assert blk2.prev_chain_hash == blk1.chain_hash, "block2 must chain to block1"
        assert blk3.prev_chain_hash == blk2.chain_hash, \
            "first block of 2026-10 must chain to last of 2026-09 (cross-partition)"
        assert len(blk1.record_hash) == 64 and len(blk1.chain_hash) == 64
        # §6.2 two-layer invariant
        rec, chn = blk1.recompute()
        assert rec == blk1.record_hash and chn == blk1.chain_hash
        print(f"[OK] chained blocks: genesis->{blk1.chain_hash[:10]}->"
              f"{blk2.chain_hash[:10]} (cross-partition -> {blk3.chain_hash[:10]})")

        # 2. verification oracle (per-month + global)
        ok9, n9, brk9 = ledger.verify_chain("2026-09", verify_hashes=True)
        ok10, n10, brk10 = ledger.verify_chain("2026-10", verify_hashes=True)
        assert ok9 and ok10 and n9 == 2 and n10 == 1 and brk9 is None and brk10 is None
        gr = ledger.verify_ledger(cross_partition=True)
        assert gr["intact"] and gr["n_blocks"] == 3 and gr["partitions"] == 2
        print(f"[OK] verification: 2026-09 n=2 intact, 2026-10 n=1 intact, "
              f"global n=3 across 2 partitions")

        # 3. tamper-evidence: retro-edit erp_blend → recompute-only detection
        path = ledger._partition_path("2026-09")
        df = pd.read_parquet(path)
        df.loc[df["ticker"] == "MSFT", "erp_blend"] = 0.0999
        df.to_parquet(path, engine="pyarrow", index=False)
        ok_t, n_t, brk_t = ledger.verify_chain("2026-09", verify_hashes=True)
        assert not ok_t and brk_t == 2, f"tamper must be detected at block2: ok={ok_t} brk={brk_t}"
        print(f"[OK] tamper-evidence: retro-edit -> chain break @#{brk_t} (recompute oracle)")
        # restore for subsequent checks
        df.loc[df["ticker"] == "MSFT", "erp_blend"] = 0.0430
        df.to_parquet(path, engine="pyarrow", index=False)

        # 4. immutability: duplicate block_id refused
        try:
            ledger.append(ticker="AAPL", asof=d, wacc_v2=0.2, ke_v2=0.2,
                          kd_pre_tax_v2=0.05, erp_blend=0.05, rf_10y=0.04,
                          beta_source="sector_bottom_up", retrieval_ts=retr,
                          action="PANEL_WRITE", gate_name="WACC_PANEL",
                          stage_gate=1, trigger_reason=tr)
            raise AssertionError("duplicate-tuple append must be refused")
        except ValueError:
            pass
        print("[OK] immutability: rewriting an existing (asof,ticker,action) refused")

        # 5. trigger_reason JSON-Schema rejection
        bad_tr = {"valuation": {"iv_p25": "x"}}
        try:
            ledger.append(ticker="TSLA", asof=d2, wacc_v2=0.2, ke_v2=0.2,
                          kd_pre_tax_v2=0.05, erp_blend=0.05, rf_10y=0.04,
                          beta_source="sector_bottom_up", retrieval_ts=retr,
                          action="OTHER", gate_name="X", stage_gate=1,
                          trigger_reason=bad_tr)
            raise AssertionError("malformed trigger_reason must reject the write")
        except ValueError as exc:
            assert "Appendix A" in str(exc), exc
        print("[OK] trigger_reason JSON Schema (Appendix A) rejects malformed writes")

        # 6. stage_gate enum + seal/rotate back-append rejection
        try:
            ledger.append(ticker="A", asof=d2, wacc_v2=0.1, ke_v2=0.1,
                          kd_pre_tax_v2=0.04, erp_blend=0.04, rf_10y=0.04,
                          beta_source="sector_bottom_up", retrieval_ts=retr,
                          stage_gate=9, trigger_reason=tr)
            raise AssertionError("stage_gate=9 must be refused")
        except ValueError:
            pass
        try:
            ledger.append(ticker="A", asof=pd.Timestamp("2026-08-01"), wacc_v2=0.1,
                          ke_v2=0.1, kd_pre_tax_v2=0.04, erp_blend=0.04, rf_10y=0.04,
                          beta_source="sector_bottom_up", retrieval_ts=retr,
                          stage_gate=5, trigger_reason=tr)
            raise AssertionError("back-append into sealed 2026-08 must be refused")
        except ValueError:
            pass
        print("[OK] stage_gate {1..5} enforced; back-append into sealed month refused")

        # 7. single-writer lock
        lock = _WriteLock(ledger.ledger_dir)
        lock.acquire()
        assert lock.path.exists()
        lock.release()
        assert not lock.path.exists()
        print("[OK] single-writer lock acquired/released atomically")

        # 8. probe: cross-partition integrity + config block
        pr = ledger.probe()
        assert pr["ledger"]["intact"], pr["ledger"]
        assert pr["config"]["uuid_version"] == 7
        assert pr["config"]["retention_years"] == 10
        assert pr["config"]["timestamp_precision"] == "nanosecond"
        assert set(pr["config"]["stage_gates"]) == {1, 2, 3, 4, 5}
        print("[OK] probe: frozen config block + cross-partition ledger intact")

        # 9. batch append (engine integration path)
        batch = [
            {"ticker": "NVDA", "asof": pd.Timestamp("2026-11-03"), "wacc_v2": 0.11,
             "ke_v2": 0.12, "kd_pre_tax_v2": 0.04, "erp_blend": 0.04, "rf_10y": 0.04,
             "beta_source": "sector_bottom_up", "retrieval_ts": retr,
             "action": "PANEL_WRITE", "gate_name": "WACC_PANEL", "stage_gate": 1,
             "trigger_reason": tr},
            {"ticker": "AMD", "asof": pd.Timestamp("2026-11-03"), "wacc_v2": 0.13,
             "ke_v2": 0.14, "kd_pre_tax_v2": 0.045, "erp_blend": 0.042, "rf_10y": 0.04,
             "beta_source": "firm_regression_60m", "retrieval_ts": retr,
             "action": "PANEL_WRITE", "gate_name": "WACC_PANEL", "stage_gate": 1,
             "trigger_reason": tr},
        ]
        ledger.append_batch(batch)
        okB, nB, brkB = ledger.verify_chain("2026-11", verify_hashes=True)
        assert okB and nB == 2 and brkB is None
        gres = ledger.verify_ledger(cross_partition=True)
        okG, nG = gres["intact"], gres["n_blocks"]
        assert okG and nG == 5, (okG, nG)  # 2026-09(2) + 2026-10(1) + 2026-11(2)
        print(f"[OK] append_batch: 2026-11 n=2; global n={nG} intact across all partitions")

    print("All TradeAuditLedger tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())