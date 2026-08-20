"""AI-cons taxonomy + screener (B-20260819-001 ruling, vector 2).

The CEO's "benefit/cons" lens: find companies that attempt to SOLVE the
problems AI creates — power draw, heat, memory bandwidth, interconnect
bottlenecks, inference cost, grid strain, water, uptime — even when AI is
never mentioned in their narrative ("it doesn't have to be mentioned").
Relevance is inferred from WHAT the company does (sector / explicit mapping),
never from how loudly it is discussed.

Deterministic: the con taxonomy is a fixed, pre-registered tuple; no RNG, no
LLM-invented scope. The quantitative gate is the sole value filter
("attention is not necessary if it's a steal") — a candidate is a potential
"hidden gem" only after it passes the existing quant baseline.

This module is a LEAF: it never opens a connection itself. Callers supply the
ticker->sector map and optionally the quant-gate hook.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Roster sector labels from valuation_alpha.universe.roster._SECTOR plus a few
# common aliases so the screener is robust to map shape.
_SECTOR_ALIASES = {
    "semiconductor": "semiconductor",
    "hardware_oem": "hardware_oem",
    "networking": "networking",
    "cloud_internet": "cloud_internet",
    "platform_software": "platform_software",
    "energy": "energy",
    "utilities": "energy",
    "consumer_electronics": "consumer_electronics",
    "financials": "financials",
    "healthcare": "healthcare",
}


@dataclass(frozen=True)
class Con:
    """A pre-registered AI con with the sectors/names that address it."""

    con_id: str
    label: str
    sectors: Tuple[str, ...] = ()
    tickers: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()


# Fixed, pre-registered taxonomy (deterministic, auditable, gate-consistent).
AI_CONS: Tuple[Con, ...] = (
    Con(
        "power_demand",
        "Datacenter power draw (generation, nuclear/SMR, grid)",
        sectors=("energy", "utilities"),
        keywords=("nuclear", "smr", "power", "grid", "transformer", "datacenter power"),
    ),
    Con(
        "cooling",
        "AI compute heat (liquid/immersion cooling, HVAC)",
        sectors=("hardware_oem",),
        keywords=("liquid cooling", "immersion cooling", "thermal", "cooling", "hvac"),
    ),
    Con(
        "memory_bandwidth",
        "HBM / memory bandwidth shortage (HBM, advanced packaging)",
        sectors=("semiconductor",),
        keywords=("hbm", "memory", "dram", "advanced packaging", "bandwidth"),
    ),
    Con(
        "interconnect",
        "Interconnect bottleneck (optical, CPO, networking)",
        sectors=("networking",),
        keywords=("optical", "cpo", "co-packaged optics", "interconnect", "ethernet"),
    ),
    Con(
        "inference_cost",
        "Inference cost (ASIC, edge AI, custom silicon)",
        sectors=("semiconductor",),
        keywords=("asic", "inference", "edge ai", "custom silicon", "cost per token"),
    ),
    Con(
        "grid_strain",
        "Grid / electrical infrastructure strain",
        sectors=("energy",),
        keywords=("grid", "transformer", "substation", "electrical infrastructure", "utilities"),
    ),
    Con(
        "water",
        "Datacenter water consumption / recovery",
        sectors=(),
        keywords=("water", "cooling water", "water recovery", "water reuse"),
    ),
    Con(
        "uptime",
        "Datacenter uptime / reliability (backup power, UPS, batteries)",
        sectors=(),
        keywords=("ups", "backup power", "battery", "reliability", "uptime"),
    ),
    Con(
        "raw_materials",
        "Raw-material bottlenecks (rare earths, substrates, helium)",
        sectors=("energy",),
        keywords=("rare earth", "substrate", "helium", "raw material", "mining"),
    ),
    Con(
        "manufacturing",
        "Manufacturing footprint (fabs, cleanroom, test equipment)",
        sectors=("semiconductor", "hardware_oem"),
        keywords=("fab", "cleanroom", "wafer", "test equipment", "capital equipment"),
    ),
)

_CONS_BY_ID = {c.con_id: c for c in AI_CONS}


def get_con(con_id: str) -> Con:
    """Return the Con for ``con_id`` or raise KeyError (deterministic)."""
    return _CONS_BY_ID[con_id]


def sector_for(ticker: str, sector_map: Optional[Dict[str, str]]) -> Optional[str]:
    """Resolve a ticker's normalized sector label, or None."""
    if not sector_map:
        return None
    raw = str(sector_map.get(str(ticker).upper(), "")).strip().lower()
    if not raw:
        return None
    return _SECTOR_ALIASES.get(raw, raw)


def cons_addressed_by(
    ticker: str,
    sector_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    """List the con_ids a ticker addresses (deterministic, no text required).

    A ticker addresses a con when (a) it is explicitly listed in the con's
    ``tickers`` set, or (b) its sector is in the con's ``sectors`` set. The
    ``keywords`` tuple exists for textual evidence only; it is never required.
    """
    t = str(ticker).strip().upper()
    sec = sector_for(t, sector_map)
    hits: List[str] = []
    for con in AI_CONS:
        if t in con.tickers or (sec and sec in con.sectors):
            hits.append(con.con_id)
    return hits


def classify_cons(text: str) -> List[str]:
    """Match con keywords against provided evidence text (optional, secondary).

    Used to corroborate a sector match, never to invent one. Deterministic:
    lowercase token match on the fixed keyword tuples.
    """
    if not text:
        return []
    blob = text.lower()
    hits: List[str] = []
    for con in AI_CONS:
        if any(kw.lower() in blob for kw in con.keywords):
            hits.append(con.con_id)
    return hits


def screen_ai_cons(
    candidates: Sequence[str],
    sector_map: Optional[Dict[str, str]] = None,
    quant_gate: Optional[Callable[[Sequence[str]], Dict[str, str]]] = None,
) -> Dict[str, Dict[str, object]]:
    """Screen candidates for AI-con relevance AND the quantitative gate.

    Returns {ticker: {cons: [...], quant_pass: bool, quant_reason: str}}.
    ``quant_gate`` is the existing ``_quant_baseline_gate``-shaped hook
    (returns {ticker: reason} for FAILURES). When no hook is supplied, quant
    pass is reported as ``unknown`` — the caller must wire the real gate.
    """
    tickers = sorted({str(t).strip().upper() for t in candidates})
    fails: Dict[str, str] = {}
    if quant_gate is not None:
        try:
            fails = quant_gate(tickers) or {}
        except Exception:  # noqa: BLE001 - fail closed to 'unknown'
            fails = {"__error__": "quant_gate_failed"}

    out: Dict[str, Dict[str, object]] = {}
    for t in tickers:
        cons = cons_addressed_by(t, sector_map)
        if t in fails:
            out[t] = {"cons": cons, "quant_pass": False, "quant_reason": fails[t]}
        elif "__error__" in fails:
            out[t] = {"cons": cons, "quant_pass": False, "quant_reason": "quant_gate_failed"}
        else:
            out[t] = {"cons": cons, "quant_pass": True, "quant_reason": ""}
    return out


def hidden_gems(
    candidates: Sequence[str],
    sector_map: Optional[Dict[str, str]] = None,
    quant_gate: Optional[Callable[[Sequence[str]], Dict[str, str]]] = None,
    held: Sequence[str] = (),
) -> List[Dict[str, object]]:
    """The 'steal' filter: con-addressing candidates that pass quant and are
    not already held. No attention/narrative multiplier by design."""
    screened = screen_ai_cons(candidates, sector_map, quant_gate)
    held_set = {str(t).strip().upper() for t in held}
    gems: List[Dict[str, object]] = []
    for t in sorted(screened):
        info = screened[t]
        if info["cons"] and info["quant_pass"] and t not in held_set:
            gems.append({"ticker": t, "cons": info["cons"]})
    return gems