"""
Startup Hook — Auto-initializes Agent Orchestrator with Experiment Context
Import this FIRST in every entry point, CLI, notebook, or agent session.
"""

# This module runs initialization on import — ensuring experiment context
# is active for all downstream logging and agent routing.

from agent_orchestrator import initialize_agent_system

# Initialize the global agent system with the pre-registered experiment
# This injects experiment-design metadata into ALL log records globally
_orchestrator = initialize_agent_system()

# Export for explicit access if needed
__all__ = ["_orchestrator", "initialize_agent_system", "get_orchestrator", "route_to_executor", "route_to_specialist"]