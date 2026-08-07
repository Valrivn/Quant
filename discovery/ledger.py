"""Provenance trail for the discovery feed (D-20260806-001 P1, SEC 5).

Pure-Python domain layer for the chain
``mentions -> candidates -> gate_passes -> integration_decisions``. Each row
carries ``source_id``, ``fetch_ts``, ``decision_ts`` and ``reason_codes`` so the
audit module can trace any number to its inputs (blueprint invariant 2).

Persistence lives in ``db/schema_discovery.py``; this module is the in-memory
domain layer and does not open connections itself.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Mention:
    """A raw mention ingested from a source."""

    source_id: str
    entity: str
    topic: str
    fetch_ts: int
    source_confidence: float
    volume_or_rank: Optional[float] = None
    sentiment: Optional[float] = None
    external_id: Optional[str] = None


@dataclass
class Candidate:
    """A mention promoted to a candidate (post ticker validation)."""

    mention: Mention
    decision_ts: int
    reason_codes: List[str] = field(default_factory=list)
    ticker: Optional[str] = None


@dataclass
class GatePass:
    """A candidate's result through the full pipeline gate."""

    candidate: Candidate
    decision_ts: int
    passed: bool
    reason_codes: List[str] = field(default_factory=list)


@dataclass
class IntegrationDecision:
    """The final decision for a gate-passed candidate."""

    gate_pass: GatePass
    decision_ts: int
    decision: str  # rejected | research_only | gated_candidate
    reason_codes: List[str] = field(default_factory=list)


class ProvenanceLedger:
    """In-memory provenance chain builder.

    Deterministic: no randomness. ``decision_ts`` is supplied by the caller so
    tests can pin timestamps.
    """

    def __init__(self) -> None:
        self.mentions: List[Mention] = []
        self.candidates: List[Candidate] = []
        self.gate_passes: List[GatePass] = []
        self.integration_decisions: List[IntegrationDecision] = []

    def add_mention(self, mention: Mention) -> Mention:
        self.mentions.append(mention)
        return mention

    def promote_to_candidate(
        self, mention: Mention, decision_ts: int,
        reason_codes: Optional[List[str]] = None, ticker: Optional[str] = None,
    ) -> Candidate:
        cand = Candidate(
            mention=mention, decision_ts=decision_ts,
            reason_codes=list(reason_codes or []), ticker=ticker,
        )
        self.candidates.append(cand)
        return cand

    def record_gate_pass(
        self, candidate: Candidate, decision_ts: int, passed: bool,
        reason_codes: Optional[List[str]] = None,
    ) -> GatePass:
        gp = GatePass(
            candidate=candidate, decision_ts=decision_ts, passed=passed,
            reason_codes=list(reason_codes or []),
        )
        self.gate_passes.append(gp)
        return gp

    def record_integration_decision(
        self, gate_pass: GatePass, decision_ts: int, decision: str,
        reason_codes: Optional[List[str]] = None,
    ) -> IntegrationDecision:
        dec = IntegrationDecision(
            gate_pass=gate_pass, decision_ts=decision_ts, decision=decision,
            reason_codes=list(reason_codes or []),
        )
        self.integration_decisions.append(dec)
        return dec

    def trace(self, entity: str) -> List[dict]:
        """Return the full provenance chain for ``entity`` as a list of dicts.

        Each dict is a stage: ``mention``, ``candidate``, ``gate_pass`` or
        ``integration_decision``, carrying source_id / fetch_ts / decision_ts /
        reason_codes. Deterministic ordering by insertion.
        """
        chain: List[dict] = []
        for m in self.mentions:
            if m.entity != entity:
                continue
            chain.append({
                "stage": "mention",
                "source_id": m.source_id,
                "entity": m.entity,
                "topic": m.topic,
                "fetch_ts": m.fetch_ts,
                "reason_codes": [],
            })
            for c in self.candidates:
                if c.mention is m:
                    chain.append({
                        "stage": "candidate",
                        "source_id": m.source_id,
                        "entity": m.entity,
                        "decision_ts": c.decision_ts,
                        "reason_codes": list(c.reason_codes),
                    })
                    for gp in self.gate_passes:
                        if gp.candidate is c:
                            chain.append({
                                "stage": "gate_pass",
                                "source_id": m.source_id,
                                "entity": m.entity,
                                "decision_ts": gp.decision_ts,
                                "passed": gp.passed,
                                "reason_codes": list(gp.reason_codes),
                            })
                            for dec in self.integration_decisions:
                                if dec.gate_pass is gp:
                                    chain.append({
                                        "stage": "integration_decision",
                                        "source_id": m.source_id,
                                        "entity": m.entity,
                                        "decision_ts": dec.decision_ts,
                                        "decision": dec.decision,
                                        "reason_codes": list(dec.reason_codes),
                                    })
        return chain