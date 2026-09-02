"""Isolated RNG utilities for the discovery layer (ruling D-20260828-001).

Determinism contract
---------------------
The discovery layer must be deterministic and is audited by
``tests/test_discovery_ranker.py::TestNoRngAudit`` (a CI grep-guard that
forbids ``random`` / ``np.random`` / ``epsilon`` tokens in every top-level
``discovery/*.py`` module). This module is the ONE sanctioned exception and is
explicitly whitelisted in that audit. It owns the only two legitimate uses of
randomness in discovery:

1. ``randomized_rank`` -- the seeded, minimized "slowly explore" re-rank for
   Thread-B merge ORDER. It never fabricates or drops nodes; it only re-orders,
   and is fully reproducible for a fixed ``seed``.
2. ``retry_jitter`` -- a small backoff jitter for transient SPARQL failures.
   Timing-only; never feeds back into discovery results.

Anything that needs randomness and lives outside this module is a bug.
"""

import random
from typing import Any, List, Mapping, Sequence

_DEFAULT_SEED = 20260828
_DEFAULT_TEMPERATURE = 0.05


def randomized_rank(nodes: Sequence[Any], randomized: Mapping[str, Any]) -> List[Any]:
    """Seeded, minimized Gaussian re-rank over a node ordering.

    Only re-orders; never adds/removes/fabricates nodes. ``temperature`` in
    (0, inf) controls how strongly the base grade dominates: larger temperature
    = more uniform; small temperature keeps near-tie perturbation only (the
    CEO's 'slowly explore' setting). Reproducible for a fixed seed.
    """
    seed = int(randomized.get("seed", _DEFAULT_SEED))
    temperature = float(randomized.get("temperature", _DEFAULT_TEMPERATURE))
    if temperature <= 0:
        return list(nodes)
    rng = random.Random(seed)

    def key(node: Any) -> float:
        base = float(node.grade)
        noise = rng.gauss(0.0, temperature)
        return base + noise

    return sorted(nodes, key=key, reverse=True)


def retry_jitter(max_jitter: float = 0.5) -> float:
    """Return uniform jitter in ``[0, max_jitter)`` for staggered backoff.

    Only spreads retry sleeps; does not influence discovery results.
    """
    return random.uniform(0, max_jitter)