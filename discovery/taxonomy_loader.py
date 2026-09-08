"""Extended XBRL taxonomy loader — merges sector chains into the base parser.

Integrates ``sector_xbrl_chains.py`` concept definitions with the existing
``xbrl_parser.py`` CONCEPT_CHAINS, providing a unified interface for
sector-aware extraction.

This module is the SINGLE INTEGRATION POINT between sector concept chains
and the screen-worker pipelines (PIT scraper, valuation_alpha pipeline,
discovery screens).
"""

from __future__ import annotations

import copy
import logging
from typing import Dict, List, Optional

from discovery.sector_xbrl_chains import (
    ALL_SECTOR_CONCEPTS,
    ALL_SECTOR_RELATIONS,
    ConceptNode,
    Relation,
    RelationType,
    Sector,
    SubSector,
    build_xbrl_concept_map,
    get_concepts_by_sector,
    get_concepts_by_subsector,
    validate_concept_graph,
)
from discovery.xbrl_parser import CONCEPT_CHAINS

logger = logging.getLogger(__name__)


class SectorTaxonomyLoader:
    """Unified loader that merges base and sector-specific XBRL concept chains.

    Usage::

        loader = SectorTaxonomyLoader()
        chains = loader.get_concept_chains()       # unified dict
        financial_chains = loader.get_concept_chains(sector=Sector.FINANCIAL)
        node = loader.get_node("net_interest_income")
        errors = loader.validate()
    """

    def __init__(self):
        self._base_chains: Dict[str, List[str]] = copy.deepcopy(CONCEPT_CHAINS)
        self._sector_chains: Dict[str, List[str]] = build_xbrl_concept_map()
        self._nodes: Dict[str, ConceptNode] = dict(ALL_SECTOR_CONCEPTS)
        self._relations: List[Relation] = list(ALL_SECTOR_RELATIONS)
        self._validation_errors: Optional[List[str]] = None

    # ── Concept chain access ──────────────────────────────────────────────

    def get_concept_chains(
        self,
        sector: Optional[Sector] = None,
        subsector: Optional[SubSector] = None,
        include_base: bool = True,
    ) -> Dict[str, List[str]]:
        """Return the concept chain mapping, optionally filtered.

        Args:
            sector: Filter to a sector. None = all sectors.
            subsector: Filter to a sub-sector (overrides sector).
            include_base: Whether to include base (non-sector) chains.
        """
        result: Dict[str, List[str]] = {}

        if include_base:
            result.update(self._base_chains)

        if subsector:
            filtered = {
                k: v for k, v in self._sector_chains.items()
                if subsector in (self._nodes[k].sub_sectors if k in self._nodes else ())
            }
            result.update(filtered)
        elif sector:
            filtered = {
                k: v for k, v in self._sector_chains.items()
                if self._nodes.get(k, ConceptNode(concept_key=k, chain=())).sector == sector
            }
            result.update(filtered)
        else:
            result.update(self._sector_chains)

        return result

    def get_concept_keys(
        self,
        sector: Optional[Sector] = None,
        subsector: Optional[SubSector] = None,
    ) -> List[str]:
        """Return all concept keys, optionally filtered."""
        if subsector:
            return sorted(get_concepts_by_subsector(subsector).keys())
        elif sector:
            return sorted(get_concepts_by_sector(sector).keys())
        return sorted(self._nodes.keys())

    # ── Individual concept access ─────────────────────────────────────────

    def get_node(self, concept_key: str) -> Optional[ConceptNode]:
        """Return the ConceptNode for a key."""
        return self._nodes.get(concept_key)

    def get_chain(self, concept_key: str) -> List[str]:
        """Return the XBRL tag chain for a concept, or empty list."""
        node = self._nodes.get(concept_key)
        if node:
            return list(node.chain)
        # Fall back to base chains
        return list(self._base_chains.get(concept_key, []))

    def get_provenance(self, concept_key: str) -> Optional[Dict]:
        """Return provenance info for a concept as a dict."""
        node = self._nodes.get(concept_key)
        if not node:
            return None
        p = node.provenance
        return {
            "source": p.source.value,
            "reference": p.reference,
            "version": p.version,
            "notes": p.notes,
        }

    # ── Relationship graph access ─────────────────────────────────────────

    def get_children(self, parent_key: str) -> List[str]:
        """Return concept keys that are children of parent_key."""
        return [
            r.from_concept for r in self._relations
            if r.to_concept == parent_key and r.relation_type in (
                RelationType.CALC_SUM, RelationType.CALC_WEIGHTED
            )
        ]

    def get_parents(self, child_key: str) -> List[str]:
        """Return concept keys that aggregate child_key."""
        return [
            r.to_concept for r in self._relations
            if r.from_concept == child_key and r.relation_type in (
                RelationType.CALC_SUM, RelationType.CALC_WEIGHTED
            )
        ]

    def get_relations(self, concept_key: str) -> List[Dict]:
        """Return all relations for a concept as dicts."""
        return [
            {
                "from": r.from_concept,
                "to": r.to_concept,
                "type": r.relation_type.value,
                "weight": r.weight,
            }
            for r in self._relations
            if r.from_concept == concept_key or r.to_concept == concept_key
        ]

    def get_derived_ratios(self) -> List[str]:
        """Return all derived ratio concept keys."""
        return sorted(set(
            r.to_concept for r in self._relations
            if r.relation_type == RelationType.DERIVED
        ))

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> List[str]:
        """Validate the concept graph and cache results."""
        if self._validation_errors is None:
            self._validation_errors = validate_concept_graph()
            if self._validation_errors:
                logger.warning(
                    "Concept graph validation found %d errors",
                    len(self._validation_errors),
                )
        return list(self._validation_errors)

    # ── Merge with base parser ────────────────────────────────────────────

    def merged_chains_for_parser(
        self,
        sector: Optional[Sector] = None,
        subsector: Optional[SubSector] = None,
    ) -> Dict[str, List[str]]:
        """Return a merged dict suitable for use in xbrl_parser functions.

        This produces a combined dict of base + sector chains where sector
        chains take precedence for overlapping keys (since they have
        sector-specific ordering and provenance).
        """
        base = copy.deepcopy(self._base_chains)
        sector_map = self.get_concept_chains(sector=sector, subsector=subsector, include_base=False)
        base.update(sector_map)
        return base

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict:
        """Serialize the entire taxonomy to a dict (for JSON/YAML export)."""
        concepts = {}
        for k, v in sorted(self._nodes.items()):
            concepts[k] = {
                "chain": list(v.chain),
                "unit": v.unit,
                "sector": v.sector.value,
                "sub_sectors": [s.value for s in v.sub_sectors],
                "is_computed": v.is_computed,
                "parent_concept": v.parent_concept,
                "provenance": {
                    "source": v.provenance.source.value,
                    "reference": v.provenance.reference,
                    "version": v.provenance.version,
                },
            }

        relations = []
        for r in self._relations:
            relations.append({
                "from": r.from_concept,
                "to": r.to_concept,
                "type": r.relation_type.value,
                "weight": r.weight,
                "provenance": {
                    "source": r.provenance.source.value,
                    "reference": r.provenance.reference,
                },
            })

        return {
            "concepts": concepts,
            "relations": relations,
            "derived_ratios": self.get_derived_ratios(),
            "validation_errors": self.validate(),
        }

    # ── Summary ───────────────────────────────────────────────────────────

    def summary(self) -> Dict:
        """Return a summary of the taxonomy state."""
        by_sector = {}
        for k, v in self._nodes.items():
            s = v.sector.value
            if s not in by_sector:
                by_sector[s] = {"total": 0, "with_chain": 0, "computed": 0}
            by_sector[s]["total"] += 1
            if v.chain:
                by_sector[s]["with_chain"] += 1
            if v.is_computed:
                by_sector[s]["computed"] += 1

        return {
            "total_concepts": len(self._nodes),
            "total_relations": len(self._relations),
            "base_chain_count": len(self._base_chains),
            "sector_chain_count": len(self._sector_chains),
            "by_sector": by_sector,
            "sub_sectors": [s.value for s in sorted(
                set(s for v in self._nodes.values() for s in v.sub_sectors),
                key=lambda x: x.value,
            )],
            "derived_ratios": self.get_derived_ratios(),
            "validation_errors": self.validate(),
        }
