"""L3 whole-portfolio allocator: LLM-guided gradient-descent weight optimizer.

The package provides a numeric core (allocator), a rule-based LLM proposal
layer (llm_guide), and a CEO-facing report (report). All modules are offline,
deterministic, and numpy/pandas only.
"""

__version__ = "0.1.0"