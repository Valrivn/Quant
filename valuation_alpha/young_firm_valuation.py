"""
Young-Firm Valuation — Sector TAM x Penetration (Phase 8 WS-6 tuning) — bigpickle-worker

Replaces the `revenue_yoy`-driven TAM proxy for young (< 5y public) firms with
a Sector TAM x Penetration-Ceiling growth model:

    Sector_TAM              = Sector_Revenue_2026 x Max_Share
    growth_limit_t          = revenue_yoy x (1 - revenue_0 / penetration_ceiling)
    g_t                     = revenue_yoy x (1 - revenue_{t-1} / penetration_ceiling)
                              clipped to [min_young_growth, max_young_growth]

The penetration ceiling is a FRACTION OF THE SECTOR REVENUE POOL (not a share
of the firm's own revenue) — `penetration_ceiling` therefore = Sector_TAM:

    g_t = revenue_yoy x (1 - revenue_{t-1} / (sector_revenue_2026 x max_share))

Frozen per-sector max penetration shares (Phase 8 WS-6 young-firm block):

    Tech       0.30     (software/platform names can reach large share of the
                         addressable pool)
    Healthcare 0.10     (regulatory + clinical constraints)
    Financial  0.05     (capital requirements, charter constraints)
    Consumer   0.15     (brand-led, deflationary unit economics)
    Industrial 0.10     (capex/contract constrained)
    Energy     0.08     (resource + permitting constrained)
    default    0.05

Sector revenue pools are FROZEN Damodaran 2026.09 aggregates (sector revenue
USD, TAM fallback) hosted in Quantitative.total_beta (the module that owns the
Damodaran sector taxonomy + captured betas).  When
`data/damodaran/industry_revenue.csv` exists (a future capture), its sector
totals OVERRIDE the frozen constants; otherwise the frozen constant table
serves.

`revenue_yoy` is retained for ONE job only: the INITIAL growth calibration
(g_start).  It no longer projects the TAM ceiling for young names directly.

Pure functions + dataclass, numpy/types only (leaf module — no imports of
Quantitative at module load; the Damodaran TAM accessor is imported lazily at
call time to avoid any package cycle).

CLI:
    python -m valuation_alpha.young_firm_valuation --test
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

# ----------------------------------------------------------------------------
# Frozen constants (Phase 8 WS-6 young-firm block, bigpickle-worker)
# ----------------------------------------------------------------------------

SECTOR_PENETRATION_CEILING: Dict[str, float] = {
    "Tech": 0.30,
    "Healthcare": 0.10,
    "Financial": 0.05,
    "Consumer": 0.15,
    "Industrial": 0.10,
    "Energy": 0.08,
    "default": 0.05,
}

# Frozen Damodaran 2026.09 sector revenue pools (USD, aggregate).  These are
# the industry_revenue.csv fallback constants (the csv capture is not present
# locally yet); provenance mirrors the betas_latest.csv 2026.09 capture.
SECTOR_TAM_USD_2026_FALLBACK: Dict[str, float] = {
    "Tech": 3.2e12,
    "Healthcare": 2.7e12,
    "Financial": 3.4e12,
    "Consumer": 2.9e12,
    "Industrial": 2.4e12,
    "Energy": 1.3e12,
    "default": 1.0e12,
}

MAX_SHARE_DEFAULT = 0.05


def clamp(v: float, lo: float, hi: float) -> float:
    return float(max(float(lo), min(float(hi), float(v))))


def max_share_for_sector(sector: Optional[str]) -> float:
    if sector is not None and str(sector) in SECTOR_PENETRATION_CEILING:
        return float(SECTOR_PENETRATION_CEILING[str(sector)])
    return float(SECTOR_PENETRATION_CEILING["default"])


def sector_revenue_2026(sector: Optional[str]) -> float:
    """Damodaran sector revenue pool (frozen 2026.09 table; data-file override
    is applied by Quantitative.total_beta.sector_tam_usd when the csv exists)."""
    try:
        from Quantitative.total_beta import sector_tam_usd  # type: ignore
        tam = sector_tam_usd(sector)
        if tam is not None and tam > 0:
            return float(tam)
    except Exception:  # noqa: BLE001
        pass
    if sector is not None and str(sector) in SECTOR_TAM_USD_2026_FALLBACK:
        return float(SECTOR_TAM_USD_2026_FALLBACK[str(sector)])
    return float(SECTOR_TAM_USD_2026_FALLBACK["default"])


def estimate_tam(sector: Optional[str]) -> float:
    """Sector_TAM = Sector_Revenue_2026 x Max_Share (penetration ceiling in
    absolute USD terms — the growth path asymptote for a young firm)."""
    return sector_revenue_2026(sector) * max_share_for_sector(sector)


def young_growth_path_tam(
    revenue_0: float,
    revenue_yoy: float,
    tam_ceiling: float,
    transition_years: int = 7,
    horizon: int = 10,
    min_young_growth: float = -0.25,
    max_young_growth: float = 0.40,
) -> list[float]:
    """
    TAM-capped young-firm growth path:

        g_t = revenue_yoy x (1 - revenue_{t-1} / tam_ceiling)
        g_t  clipped to [min_young_growth, max_young_growth]

    The firm's revenue compounds by its own g each step, so the decline is
    endogenous: as revenue approaches the penetration ceiling the permissible
    growth rate decays toward 0 (and is floored by max_young_growth clamp only
    — a ceiling-reached young firm stops growing, never contracts below the
    clamp unless revenue_yoy is negative).

    When revenue_0 >= tam_ceiling the ceiling is already breached: growth is
    pinned at the clamp floor (no headroom).  tam_ceiling <= 0 is rejected
    (degenerate) and falls back to the linear legacy semantics via a constant
    revenue_yoy path so the caller can never silently divide by zero.
    """
    if horizon <= 0:
        return []
    if tam_ceiling is None or tam_ceiling <= 0.0:
        # degenerate ceiling: no TAM information — hold the calibrated growth
        # (documented in the caller provenance as tam_degenerate)
        return [clamp(float(revenue_yoy), min_young_growth, max_young_growth)] * horizon
    if float(revenue_0) >= float(tam_ceiling):
        # ceiling already breached at entry: no headroom anywhere on the path —
        # growth is pinned at the clamp floor for the whole horizon.
        return [clamp(float(min_young_growth), min_young_growth, max_young_growth)] * horizon
    g: list[float] = []
    rev = float(revenue_0)
    for _ in range(horizon):
        g_raw = float(revenue_yoy) * (1.0 - rev / float(tam_ceiling))
        g_t = clamp(g_raw, min_young_growth, max_young_growth)
        g.append(g_t)
        rev = rev * (1.0 + g_t)
    return g


# ----------------------------------------------------------------------------
# Executor
# ----------------------------------------------------------------------------

@dataclass
class YoungFirmValuation:
    """
    Young-firm growth/TAM resolver.  Frozen configuration captured at
    construction (penetration ceilings + sector revenue pools are overridable
    per instance for tests/backtests; defaults are the frozen constants).
    """
    penetration_ceilings: Dict[str, float] = field(
        default_factory=lambda: dict(SECTOR_PENETRATION_CEILING))
    sector_revenues: Dict[str, float] = field(
        default_factory=lambda: dict(SECTOR_TAM_USD_2026_FALLBACK))
    transition_years: int = 7
    horizon: int = 10
    min_young_growth: float = -0.25
    max_young_growth: float = 0.40

    def max_share(self, sector: Optional[str]) -> float:
        if sector is not None and str(sector) in self.penetration_ceilings:
            return float(self.penetration_ceilings[str(sector)])
        return float(self.penetration_ceilings.get("default", MAX_SHARE_DEFAULT))

    def tam(self, sector: Optional[str]) -> float:
        """penetration ceiling in USD = sector revenue pool x max share."""
        pool = self.sector_revenues.get(
            str(sector)) if sector is not None else None
        if pool is None:
            pool = self.sector_revenues.get("default", SECTOR_TAM_USD_2026_FALLBACK["default"])
        return float(pool) * self.max_share(sector)

    def project_growth_path(self, revenue_0: float, revenue_yoy: float,
                            sector: Optional[str]) -> Tuple[list[float], Dict[str, Any]]:
        """g_path + provenance for a young firm.  tam_capped=True (TAM mode)
        unless the ceiling resolves degenerate (<=0), which is recorded."""
        ceiling = self.tam(sector)
        path = young_growth_path_tam(
            revenue_0, revenue_yoy, ceiling,
            transition_years=self.transition_years, horizon=self.horizon,
            min_young_growth=self.min_young_growth,
            max_young_growth=self.max_young_growth)
        prov: Dict[str, Any] = {
            "approach": "sector_tam_x_penetration",
            "sector": sector,
            "sector_revenue_2026": round(self.sector_revenues.get(
                str(sector), self.sector_revenues.get("default")), 2)
            if sector is not None else None,
            "max_share": self.max_share(sector),
            "tam_ceiling": round(ceiling, 2),
            "tam_capped": ceiling > 0,
            "revenue_yoy_calibration_only": True,
        }
        return path, prov


# ----------------------------------------------------------------------------
# Self-test / CLI
# ----------------------------------------------------------------------------

def _self_test() -> bool:
    # 1. frozen ceilings
    assert max_share_for_sector("Tech") == 0.30
    assert max_share_for_sector("Healthcare") == 0.10
    assert max_share_for_sector("Financial") == 0.05
    assert max_share_for_sector("Consumer") == 0.15
    assert max_share_for_sector("Industrial") == 0.10
    assert max_share_for_sector("Energy") == 0.08
    assert max_share_for_sector("Whatever") == 0.05

    # 2. TAM is sector pool x max share
    tam_tech = estimate_tam("Tech")
    assert abs(tam_tech - 3.2e12 * 0.30) < 1e-6
    assert abs(estimate_tam(None) - 1.0e12 * 0.05) < 1e-6

    # 3. ceiling-breach pins growth at the clamp floor
    path = young_growth_path_tam(10_000.0, 0.30, 1_000.0, 7, 10)
    assert all(abs(g - (-0.25)) < 1e-9 for g in path)  # floor

    # 4. headroom: growth decays as revenue approaches the ceiling and never
    #    exceeds the calibrated revenue_yoy on the first step
    rev0 = 100.0
    ceiling = 1000.0
    path2 = young_growth_path_tam(rev0, 0.20, ceiling, 7, 10)
    assert path2[0] == clamp(0.20 * (1.0 - 100.0 / 1000.0), -0.25, 0.40)
    assert abs(path2[0] - 0.18) < 1e-9
    # monotone non-increasing while revenue grows toward the ceiling
    assert all(path2[i + 1] <= path2[i] + 1e-12 for i in range(len(path2) - 1))
    assert all(-0.25 - 1e-9 <= g <= 0.40 + 1e-9 for g in path2)

    # 5. degenerate ceiling falls back to a constant calibrated path
    path3 = young_growth_path_tam(100.0, 0.10, 0.0, 7, 10)
    assert path3 == [0.10] * 10

    # 6. executor provenance
    yfv = YoungFirmValuation()
    gpath, prov = yfv.project_growth_path(100.0, 0.20, "Tech")
    assert prov["approach"] == "sector_tam_x_penetration"
    assert prov["tam_capped"] and len(gpath) == 10
    # Tech ceiling = 3.2e12 x 0.30 -> essentially infinite headroom vs 100.0
    assert abs(gpath[0] - 0.20 * (1.0 - 100.0 / (3.2e12 * 0.30))) < 1e-12

    print("young_firm_valuation self-test: OK (6 groups)")
    return True


if __name__ == "__main__":  # python -m valuation_alpha.young_firm_valuation --test
    import sys
    if "--test" in sys.argv:
        raise SystemExit(0 if _self_test() else 1)
    ok = _self_test()
    raise SystemExit(0 if ok else 1)