"""
Execution layer (Phase 0-1, Workstream B).

Entry points:
    execution.tca_simulator  — Almgren-Chriss TCA gate + LOB simulator
                              (python -m execution.tca_simulator --test --lob --budget 150)

Public names are resolved lazily (PEP 562) so that `python -m execution.<mod>`
does not pre-import submodules and trip runpy's double-import warning.
"""

__all__ = [
    "BUDGET_BPS",
    "LOBSimulator",
    "round_trip_cost_bps",
    "stress_test_concurrent",
    "tca_budget_gate",
    "schedule_staggered_exits",
    "hierarchical_cluster_groups",
]


def __getattr__(name):
    if name in __all__:
        from execution import tca_simulator as _mod
        return getattr(_mod, name)
    raise AttributeError(f"module 'execution' has no attribute {name!r}")