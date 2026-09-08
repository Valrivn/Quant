"""
Agent Orchestrator â€” Nemotron Brain â†’ Gemini/BigPickle Execution Router
Startup initialization for all agents with experiment-design context injection.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from typing import TYPE_CHECKING
from pathlib import Path

from config.logging_config import get_experiment_context, setup_logging, init_logging, college_event

logger = logging.getLogger(__name__)


# Sensory layer: agents that read the world and report state to the brain
# (they do NOT decide). Lane = CIC blueprint category each sensory agent feeds.
SENSORY_AGENT_LANES: Dict[str, str] = {
    "screen_worker": "screener",
    "datasource_worker": "ingestion",
    "edgar_worker": "ingestion",
    "sec_pit_worker": "ingestion",
    "ig_worker": "ingestion",
    "risk_automator": "risk_engine",
    "stoch_worker": "risk_engine",
    # 4-Stage Pipeline sensory agents
    "dcf_screen_worker": "screener",
    "entry_timing_worker": "screener",
    "exit_engine_worker": "screener",
    "sizing_rebalancing_worker": "risk_engine",
    "backtest_worker": "risk_engine",
    "fallback_engine_worker": "risk_engine",
    "black_swan_worker": "risk_engine",
    "hmm_regime_worker": "risk_engine",
    "portfolio_orchestrator": "risk_engine",
    # Phase 3: Consumer Signal
    "consumer_signal_worker": "screener",
}

# Default CIC blueprint locations (full + routing projection).
BLUEPRINT_PATH = "data/pipeline_cache/workspace_blueprint.json"
ROUTING_PATH = "data/pipeline_cache/workspace_routing.json"


class AgentRole(Enum):
    BRAIN = "nemotron"           # Planning, orchestration, decisions
    EXECUTOR = "gemini_bigpickle"  # Code execution, building, data pipelines
    SPECIALIST = "specialist"    # Domain-specific workers (alpha, risk, data, etc.)


@dataclass
class AgentSpec:
    name: str
    role: AgentRole
    model: str
    capabilities: List[str]
    tools: List[str] = field(default_factory=list)
    description: str = ""


# Agent Registry â€” maps task types to specialized agents
AGENT_REGISTRY: Dict[str, AgentSpec] = {
    # Brain (Nemotron - this process)
    "orchestrator": AgentSpec(
        name="orchestrator",
        role=AgentRole.BRAIN,
        model="nemotron-3-ultra",
        capabilities=["planning", "task_decomposition", "agent_routing", "decision_making", "experiment_governance"],
        description="Nemotron brain: decomposes goals, routes to executors, enforces experiment protocol",
    ),

    # Execution layer (Gemini/BigPickle via task tool)
    "alpha_integrator": AgentSpec(
        name="alpha_integrator",
        role=AgentRole.EXECUTOR,
        model="gemini-3.1-pro",
        capabilities=["alpha_decay", "weight_calibration", "portfolio_optimization", "signal_integration"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Alpha integration, weight calibration, decay modeling",
    ),
    "alpha_worker": AgentSpec(
        name="alpha_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["spy_excess", "beta_neutralization", "fama_french_passes", "factor_modeling"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="SPY-excess computation, beta-neutralization, Fama-French factor passes",
    ),
    "backtest_agent": AgentSpec(
        name="backtest_agent",
        role=AgentRole.EXECUTOR,
        model="gemini-3.1-pro",
        capabilities=["chi_square_backtest", "sharpe_sortino_calmar", "regime_returns", "audit_reporting"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "task"],
        description="Runs house backtest via backtesting.chi_square, reports metrics, writes artifacts",
    ),
    "datasource_worker": AgentSpec(
        name="datasource_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["scraper_engineering", "degraded_source_registry", "live_ingestion", "cdxj_harvest", "edgar_fetcher"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "webfetch", "websearch"],
        description="Unified scraper engineering, CDXJ/EDGAR/GDELT ingestion, source health monitoring",
    ),
    "risk_automator": AgentSpec(
        name="risk_automator",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["var_calculation", "leverage_brakes", "drawdown_circuit_breakers", "position_sizing"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="VaR, leverage brakes, drawdown circuit breakers, position sizing",
    ),
    "execution_strategist": AgentSpec(
        name="execution_strategist",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["fee_budgeting", "slippage_modeling", "execution_drift", "transaction_cost_analysis"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Transaction fee budgeting, execution drift, slippage bounds",
    ),
    "screen_worker": AgentSpec(
        name="screen_worker",
        role=AgentRole.EXECUTOR,
        model="ling-3-flash-fin-free",
        capabilities=["pit_xbrl_screens", "damodaran_fundamentals", "earnings_quality", "valuation_screens"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "webfetch"],
        description="Standard PIT XBRL screens and Damodaran fundamentals",
    ),
    "stoch_worker": AgentSpec(
        name="stoch_worker",
        role=AgentRole.SPECIALIST,
        model="gemini-3-flash",
        capabilities=["stochastic_core", "regime_shift", "frozen_models", "regime_detection"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Read-only frozen stochastic and regime-shift cores",
    ),
    "devil_advocate": AgentSpec(
        name="devil_advocate",
        role=AgentRole.SPECIALIST,
        model="gemini-3-flash",
        capabilities=["stress_test_positions", "expose_weak_assumptions", "debate_protocol"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Stress-tests Position A vs B debates; exposes weak assumptions",
    ),
    "open_minded": AgentSpec(
        name="open_minded",
        role=AgentRole.SPECIALIST,
        model="ling-3-flash-fin-free",
        capabilities=["financial_sanity_voting", "macro_assumption_challenges", "independent_review"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Financial sanity voting and macro assumption challenges",
    ),
    "educator": AgentSpec(
        name="educator",
        role=AgentRole.SPECIALIST,
        model="gemini-3-flash",
        capabilities=["statistical_translation", "feynman_explanation", "neutral_education"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Neutral statistical/financial translator â€” definitions first, no advice",
    ),
    "pipeline_supervisor": AgentSpec(
        name="pipeline_supervisor",
        role=AgentRole.SPECIALIST,
        model="mimo-v2.5-free",
        capabilities=["ops_supervision", "degradation_halt", "ingestion_lag_monitoring", "health_checks"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Central ops supervisor; halts on degradation and ingestion lags",
    ),
    "sim_guardian": AgentSpec(
        name="sim_guardian",
        role=AgentRole.SPECIALIST,
        model="gemini-3.1-pro",
        capabilities=["sim_physics", "purge_embargo", "audited_clean_stamp", "corrosion_lock"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Sim-physics, purge-and-embargo checks, AUDITED CLEAN stamps",
    ),
    "audit_worker": AgentSpec(
        name="audit_worker",
        role=AgentRole.SPECIALIST,
        model="nemotron-3.5-lightning",
        capabilities=["sha256_provenance", "corrosion_lock_verification", "data_lineage"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="SHA-256 provenance hashes and corrosion lock verification",
    ),
    "edgar_worker": AgentSpec(
        name="edgar_worker",
        role=AgentRole.SPECIALIST,
        model="mimo-v2.5-free",
        capabilities=["sec_edgar_cik_resolver", "xbrl_parsing", "companyfacts_fetch"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "webfetch"],
        description="SEC EDGAR/CIK resolver lookups (read-only)",
    ),
    "sec_pit_worker": AgentSpec(
        name="sec_pit_worker",
        role=AgentRole.SPECIALIST,
        model="mimo-v2.5-free",
        capabilities=[
            "pit_xbrl_scraper", "async_edgar_fetch", "checkpoint_resume",
            "companyfacts_parsing", "submissions_indexing", "parquet_output",
        ],
        tools=["read", "write", "edit", "bash", "grep", "glob", "webfetch"],
        description="Async SEC EDGAR PIT scraper: 10-K/10-Q XBRL fundamentals back to 1999",
    ),
    "ig_worker": AgentSpec(
        name="ig_worker",
        role=AgentRole.SPECIALIST,
        model="mimo-v2.5-free",
        capabilities=["instagram_tiktok_trends", "whisper_parsing", "social_signal_filtering"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Instagram/TikTok trend filtering and Whisper parsers (read-only)",
    ),
    "rapid_agent": AgentSpec(
        name="rapid_agent",
        role=AgentRole.SPECIALIST,
        model="nemotron-3.5-lightning",
        capabilities=["status_pinging", "health_checks", "ultra_fast_response"],
        tools=["read", "bash"],
        description="Ultra-fast status pinging (<= 60 tokens)",
    ),
    # 4-Stage Pipeline Agents (Phase 1)
    "dcf_screen_worker": AgentSpec(
        name="dcf_screen_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["dcf_screen", "capitalized_rd", "roic_calculation", "solvency_check", "margin_health", "monte_carlo_dcf"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Stage 1: DCF screening with capitalized R&D, ROIC, solvency, margin health, Monte Carlo IV distribution",
    ),
    "entry_timing_worker": AgentSpec(
        name="entry_timing_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["entry_timing", "asymmetric_corridor", "grassroots_velocity", "liquidity_gates"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Stage 2: Entry timing with asymmetric corridor, C_t grassroots cascade velocity, T_t/W_t quiet check, liquidity gates",
    ),
    "exit_engine_worker": AgentSpec(
        name="exit_engine_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["exit_engine", "take_profit", "hysteria_exit", "structural_break", "margin_decay", "eva_calculation"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Stage 3: Exit engine with take profit (+15% IV_Fair), hysteria exit (hype P95, T_t/W_t swarm), structural break (margin decay >300bps, EVA<=0)",
    ),
    "sizing_rebalancing_worker": AgentSpec(
        name="sizing_rebalancing_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["sizing_rebalancing", "conviction_sizing", "sector_caps", "regime_allocation", "drift_bands", "order_generation"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Stage 4: Dynamic position sizing with conviction multipliers, hard caps (15% single, 30% sector), regime allocation (EXP/SHOCK/CRISIS), 2% drift bands",
    ),
    "backtest_worker": AgentSpec(
        name="backtest_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3.1-pro",
        capabilities=["backtest_harness", "anova_sector", "welch_ttest", "chi_square_winloss", "dsr", "ff5_alpha", "max_drawdown"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "task"],
        description="Backtest harness: Full pipeline simulation with statistical test suite (ANOVA, Welch's t-test, Chi-square, DSR, FF5 alpha)",
    ),
    "fallback_engine_worker": AgentSpec(
        name="fallback_engine_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["fallback_allocation", "nav_gate", "liquidity_gate", "treasury_gold_allocation"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Fallback engine: Treasury (BIL/SHV) + Gold (GLDM/IAU) allocation with NAV clamping and liquidity gates",
    ),
    "black_swan_worker": AgentSpec(
        name="black_swan_worker",
        role=AgentRole.SPECIALIST,
        model="gemini-3.1-pro",
        capabilities=["fast_trigger", "slow_trigger", "moat_invariance", "sector_monitor", "hmm_integration"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Black Swan engine: Fast (5-day >3.5Ïƒ) / Slow (HMM P(Crisis)>0.40) triggers, moat invariance, 4-layer sector monitor",
    ),
    "hmm_regime_worker": AgentSpec(
        name="hmm_regime_worker",
        role=AgentRole.SPECIALIST,
        model="gemini-3-flash",
        capabilities=["hmm_regime_detection", "crisis_probability", "macro_features", "rolling_retrain"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="HMM regime detector: 3-state Gaussian HMM (Expansion/Shock/Crisis) with ordered-state identification",
    ),
    "portfolio_orchestrator": AgentSpec(
        name="portfolio_orchestrator",
        role=AgentRole.EXECUTOR,
        model="gemini-3.1-pro",
        capabilities=["orchestration_cycle", "stage_coordination", "order_generation", "regime_integration"],
        tools=["read", "write", "edit", "bash", "grep", "glob", "task"],
        description="Main orchestration loop coordinating Stages 1-4 + Fallback + HMM + Black Swan + Backtest",
    ),
    # Phase 3: Consumer Signal
    "consumer_signal_worker": AgentSpec(
        name="consumer_signal_worker",
        role=AgentRole.EXECUTOR,
        model="gemini-3-flash",
        capabilities=["consumer_signal", "c_t_computation", "gdelt_integration", "gh_archive_integration", "edgar_13f_integration", "ken_french_integration", "google_trends_integration", "kaggle_analyst_integration", "lead_lag_validation"],
        tools=["read", "write", "edit", "bash", "grep", "glob"],
        description="Phase 3: Multi-source C_t computation engine - aggregates GDELT, GH Archive, EDGAR 13F, Ken French, Google Trends, Kaggle Analyst into unified C_t velocity with lead-lag validation",
    ),
}


class AgentOrchestrator:
    """
    Nemotron Brain â†’ Gemini/BigPickle Executor Router.
    
    On startup:
    1. Initializes experiment-design context (pre-registered, frozen)
    2. Configures logging with experiment metadata injection
    3. Registers all agents with capabilities
    4. Provides routing interface for task delegation
    """
    
    def __init__(self, experiment_id: str = "sentiment-cascade-value-v3"):
        self.experiment_id = experiment_id
        self.experiment_context = get_experiment_context()
        self.agents: Dict[str, AgentSpec] = AGENT_REGISTRY.copy()
        self._task_history: List[Dict[str, Any]] = []
        self._initialized = False
        self.blueprint: Optional[Dict[str, Any]] = None
        self.routing_map: Dict[str, Any] = {}
        self.cic_loaded: bool = False
        self.cic_error: Optional[str] = None
        
    def startup(self) -> None:
        """Full startup sequence â€” call once at process initialization."""
        if self._initialized:
            logger.warning("Orchestrator already initialized")
            return
            
        # 1. Initialize logging with experiment context injection
        init_logging(level="INFO", inject_experiment=True)
        
        # 2. Verify experiment design is loaded and matches
        self._verify_experiment_context()
        
        # 3. Load the CIC workspace blueprint (degrades to registry-only)
        self._load_workspace_blueprint()
        
        # 4. Log startup banner with experiment metadata
        self._log_startup_banner()
        
        # 5. Verify agent registry health
        self._verify_agent_registry()
        
        self._initialized = True
        logger.info("AgentOrchestrator startup complete â€” experiment context active")
        college_event("orchestrator_startup", blueprint_loaded=self.cic_loaded)
    
    def _verify_experiment_context(self) -> None:
        """Verify experiment context matches the pre-registered design."""
        expected = {
            "experiment_id": "sentiment-cascade-value-v3",
            "version": "2026-09-05-rev3",
            "pre_registered": True,
        }
        for key, expected_val in expected.items():
            actual = self.experiment_context.get(key)
            if actual != expected_val:
                logger.error(
                    f"EXPERIMENT CONTEXT MISMATCH: {key}={actual} (expected {expected_val})"
                )
                raise RuntimeError(f"Experiment context verification failed: {key}")
        logger.info(f"Experiment context verified: {self.experiment_context['experiment_id']} v{self.experiment_context['version']}")
    
    def _log_startup_banner(self) -> None:
        """Log the experiment banner with all critical parameters."""
        ctx = self.experiment_context
        logger.info("=" * 60)
        logger.info(f"EXPERIMENT STARTUP: {ctx['experiment_id']} v{ctx['version']}")
        logger.info(f"  Universe Filter: {ctx['universe_filter']}")
        logger.info(f"  Entry Gate: {ctx['entry_gate']}")
        logger.info(f"  Rebalance: {ctx['rebalance_cadence']}")
        logger.info(f"  Cost: {ctx['cost_bps']}bps round-trip")
        logger.info(f"  MaxDD: {ctx['max_drawdown']:.0%}")
        logger.info(f"  Tiers: {ctx['tiers']}")
        logger.info("=" * 60)
    
    def _verify_agent_registry(self) -> None:
        """Verify all registered agents have required fields."""
        for name, spec in self.agents.items():
            if not spec.capabilities:
                logger.warning(f"Agent {name} has no capabilities defined")
            if spec.role == AgentRole.EXECUTOR and not spec.tools:
                logger.warning(f"Executor {name} has no tools defined")
        logger.info(f"Agent registry verified: {len(self.agents)} agents registered")
    
    def _load_workspace_blueprint(self) -> None:
        """Load the CIC routing projection (fallback: full blueprint).

        Degrades gracefully to registry-only routing when the blueprint is
        missing or stale â€” boot must never fail because of it.
        """
        for p in (ROUTING_PATH, BLUEPRINT_PATH):
            path = Path(p)
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                if "modules" not in payload:
                    continue
                if p == ROUTING_PATH:
                    self.routing_map = payload.get("modules", {})
                self.blueprint = payload
                self.cic_loaded = True
                logger.info("CIC blueprint loaded: %s (%d modules)", p, len(self._blueprint_modules()))
                college_event("cic_blueprint_loaded", path=p, modules=len(self._blueprint_modules()))
                return
            except Exception as exc:
                self.cic_error = f"{type(exc).__name__}: {exc}"
                logger.warning("CIC blueprint load failed for %s: %s", p, exc)
        self.cic_loaded = False
        logger.warning(
            "CIC blueprint not found (%s / %s) â€” orchestrator running registry-only",
            ROUTING_PATH, BLUEPRINT_PATH,
        )
        college_event("cic_blueprint_missing", paths=[ROUTING_PATH, BLUEPRINT_PATH])

    def _blueprint_modules(self) -> List[Dict[str, Any]]:
        """Return blueprint module rows (routing map or full rows)."""
        if self.routing_map:
            return [dict(path=p, code=c[0] if isinstance(c, list) else c)
                    for p, c in self.routing_map.items()]
        return self.blueprint.get("modules", []) if self.blueprint else []

    # ------------------------------------------------------------------ sensory layer

    def sensory_agents(self) -> List[AgentSpec]:
        """Agents that report world-state to the brain (read-only sensory layer)."""
        return [self.agents[n] for n in SENSORY_AGENT_LANES if n in self.agents]

    def lane_for(self, agent_name: str) -> Optional[str]:
        """Map an agent to its CIC lane (None = not a sensory agent)."""
        return SENSORY_AGENT_LANES.get(agent_name)

    def lane_resources(self, lane: str) -> List[Dict[str, Any]]:
        """Blueprint capabilities available in a lane (+ their state)."""
        if not self.cic_loaded:
            return []
        out = []
        for path, code in self.routing_map.items():
            c = code[0] if isinstance(code, list) and code else code
            if isinstance(c, str) and c and c[0] == lane[0]:
                out.append({"path": path, "state": c[1] if len(c) > 1 else "?"})
        return out

    def sensory_summary(self) -> Dict[str, Any]:
        """Compact state snapshot for the brain's context window."""
        lanes = {}
        for agent, lane in SENSORY_AGENT_LANES.items():
            if agent not in self.agents:
                continue
            lanes[lane] = lanes.get(lane, {"agents": [], "modules": []})
            lanes[lane]["agents"].append(agent)
            lanes[lane]["modules"] = [
                m["path"] for m in self.lane_resources(lane)[:5]
            ]
        return {
            "cic_loaded": self.cic_loaded,
            "blueprint_error": self.cic_error,
            "sensory_lanes": lanes,
        }
    
    def route_task(
        self,
        task_type: str,
        payload: Dict[str, Any],
        priority: str = "normal",
        require_consensus: bool = False,
    ) -> Dict[str, Any]:
        """
        Route a task to the appropriate agent(s).
        
        Args:
            task_type: Type of task (matches agent capabilities)
            payload: Task data and parameters
            priority: "low" | "normal" | "high" | "critical"
            require_consensus: If True, route to multiple agents for consensus
            
        Returns:
            Dict with routing info and agent assignment
        """
        # Inject experiment context into payload
        payload = {**payload, "experiment_context": self.experiment_context}
        
        # Find matching agents
        matching = [
            (name, spec) for name, spec in self.agents.items()
            if task_type in spec.capabilities or any(task_type in cap for cap in spec.capabilities)
        ]
        
        if not matching:
            # Fallback: try role-based routing
            matching = [
                (name, spec) for name, spec in self.agents.items()
                if spec.role == AgentRole.EXECUTOR
            ]
        
        if not matching:
            raise ValueError(f"No agent found for task_type: {task_type}")
        
        # Select primary agent (first match) + consensus agents if needed
        primary_name, primary_spec = matching[0]
        consensus_agents = [name for name, _ in matching[1:3]] if require_consensus else []
        
        # Brain annotation: sensory lane (if the primary is a sensory agent),
        # plus blueprint module coverage for that lane.
        lane = self.lane_for(primary_name)
        lane_modules = self.lane_resources(lane) if lane else []
        
        routing = {
            "task_id": f"{task_type}_{len(self._task_history)}",
            "task_type": task_type,
            "primary_agent": primary_name,
            "primary_model": primary_spec.model,
            "primary_role": primary_spec.role.value,
            "consensus_agents": consensus_agents,
            "sensory_lane": lane,
            "lane_module_count": len(lane_modules),
            "payload": payload,
            "priority": priority,
            "experiment_context": self.experiment_context,
        }
        
        self._task_history.append(routing)
        logger.info(f"Routed {task_type} -> {primary_name} ({primary_spec.model})" +
                    (f" + consensus: {consensus_agents}" if consensus_agents else ""))
        
        # College audit: every routing decision is a machine-readable record.
        college_event(
            "routing_decision",
            task_type=task_type,
            agent=primary_name,
            model=primary_spec.model,
            sensory_lane=lane,
            lane_module_count=len(lane_modules),
            consensus=",".join(consensus_agents) if consensus_agents else "",
            priority=priority,
            blueprint_loaded=self.cic_loaded,
        )
        
        return routing
    
    def get_agent(self, name: str) -> Optional[AgentSpec]:
        """Get agent spec by name."""
        return self.agents.get(name)
    
    def list_agents(self, role: Optional[AgentRole] = None) -> List[AgentSpec]:
        """List all agents, optionally filtered by role."""
        agents = list(self.agents.values())
        if role:
            agents = [a for a in agents if a.role == role]
        return agents
    
    def get_experiment_context(self) -> Dict[str, Any]:
        """Get the frozen experiment context for injection into tasks."""
        return self.experiment_context.copy()
    
    def log_task_completion(self, task_id: str, result: Dict[str, Any], success: bool) -> None:
        """Log task completion for audit trail."""
        logger.info(
            f"Task {task_id} {'completed' if success else 'FAILED'}: "
            f"keys={list(result.keys()) if isinstance(result, dict) else type(result)}"
        )
        # Could persist to signal_provenance table here

    # â”€â”€â”€ Pipeline Stage Routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def run_dcf_screen(self, tickers: Optional[List[str]] = None) -> Dict[str, Any]:
        """Route DCF screen task to dcf_screen_worker."""
        return self.route_task(
            task_type="dcf_screen",
            payload={"tickers": tickers},
            priority="high",
        )

    def run_entry_timing(
        self,
        tickers: Optional[List[str]] = None,
        prices: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Route entry timing task to entry_timing_worker."""
        return self.route_task(
            task_type="entry_timing",
            payload={"tickers": tickers, "prices": prices},
            priority="high",
        )

    def run_fallback_engine(self, regime: str = "NORMAL") -> Dict[str, Any]:
        """Route fallback engine task to fallback_engine_worker."""
        return self.route_task(
            task_type="fallback_allocation",
            payload={"regime": regime},
            priority="high",
        )

    def run_black_swan_check(
        self,
        tickers: List[str],
        prices_dict: Dict[str, Any],
        current_prices: Dict[str, float],
    ) -> Dict[str, Any]:
        """Route black swan check to black_swan_worker."""
        return self.route_task(
            task_type="black_swan_check",
            payload={
                "tickers": tickers,
                "prices_dict": prices_dict,
                "current_prices": current_prices,
            },
            priority="critical",
        )

    def run_hmm_regime(self) -> Dict[str, Any]:
        """Route HMM regime detection to hmm_regime_worker."""
        return self.route_task(
            task_type="hmm_regime_detection",
            payload={},
            priority="high",
        )

    def run_orchestration_cycle(self, portfolio_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run full orchestration cycle via portfolio_orchestrator."""
        return self.route_task(
            task_type="orchestration_cycle",
            payload={"portfolio_state": portfolio_state},
            priority="critical",
        )

    # â”€â”€â”€ Phase 2 Pipeline Routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def run_exit_engine(
        self,
        tickers: List[str],
        current_prices: Dict[str, float],
    ) -> Dict[str, Any]:
        """Route exit engine to exit_engine_worker."""
        return self.route_task(
            task_type="exit_engine",
            payload={"tickers": tickers, "current_prices": current_prices},
            priority="high",
        )

    def run_sizing_rebalancing(
        self,
        portfolio_state: Dict[str, Any],
        dcf_results: Dict[str, Any],
        entry_signals: Dict[str, Any],
        exit_signals: Dict[str, Any],
        black_swan_decisions: Dict[str, Any],
        fallback_weights: Dict[str, float],
        regime: str,
        hmm_crisis_prob: float,
        current_prices: Dict[str, float],
    ) -> Dict[str, Any]:
        """Route sizing & rebalancing to sizing_rebalancing_worker."""
        return self.route_task(
            task_type="sizing_rebalancing",
            payload={
                "portfolio_state": portfolio_state,
                "dcf_results": dcf_results,
                "entry_signals": entry_signals,
                "exit_signals": exit_signals,
                "black_swan_decisions": black_swan_decisions,
                "fallback_weights": fallback_weights,
                "regime": regime,
                "hmm_crisis_prob": hmm_crisis_prob,
                "current_prices": current_prices,
            },
            priority="high",
        )

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        universe: Optional[List[str]] = None,
        initial_capital: float = 1_000_000,
    ) -> Dict[str, Any]:
        """Route backtest to backtest_worker."""
        if universe is None:
            universe = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
                "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
                "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
                "PFE", "TMO", "ABBV", "ACN", "COST"
            ]
        return self.route_task(
            task_type="backtest_harness",
            payload={
                "start_date": start_date,
                "end_date": end_date,
                "universe": universe,
                "initial_capital": initial_capital,
            },
            priority="normal",
        )

    def run_consumer_signal(
        self,
        tickers: Optional[List[str]] = None,
        lookback_days: int = 60,
    ) -> Dict[str, Any]:
        """Route consumer signal (C_t computation) to consumer_signal_worker."""
        if tickers is None:
            tickers = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
                "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
                "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
                "PFE", "TMO", "ABBV", "ACN", "COST"
            ]
        return self.route_task(
            task_type="consumer_signal",
            payload={"tickers": tickers, "lookback_days": lookback_days},
            priority="high",
        )


# Global orchestrator instance (initialized at import)
_orchestrator: Optional[AgentOrchestrator] = None


def get_orchestrator() -> AgentOrchestrator:
    """Get the global orchestrator instance, initializing if needed."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgentOrchestrator()
        _orchestrator.startup()
    return _orchestrator


def route_to_executor(task_type: str, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Convenience function: route task to executor layer (Gemini/BigPickle)."""
    return get_orchestrator().route_task(task_type, payload, **kwargs)


def route_to_specialist(task_type: str, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Convenience function: route task to specialist agent."""
    return get_orchestrator().route_task(task_type, payload, **kwargs)


# Pipeline convenience functions
def run_dcf_screen(tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run Stage 1: DCF Screen."""
    return get_orchestrator().run_dcf_screen(tickers)


def run_entry_timing(
    tickers: Optional[List[str]] = None,
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Run Stage 2: Entry Timing."""
    return get_orchestrator().run_entry_timing(tickers, prices)


def run_fallback_engine(regime: str = "NORMAL") -> Dict[str, Any]:
    """Run Fallback Engine."""
    return get_orchestrator().run_fallback_engine(regime)


def run_black_swan_check(
    tickers: List[str],
    prices_dict: Dict[str, Any],
    current_prices: Dict[str, float],
) -> Dict[str, Any]:
    """Run Black Swan Check."""
    return get_orchestrator().run_black_swan_check(tickers, prices_dict, current_prices)


def run_hmm_regime() -> Dict[str, Any]:
    """Run HMM Regime Detection."""
    return get_orchestrator().run_hmm_regime()


def run_consumer_signal(
    tickers: Optional[List[str]] = None,
    lookback_days: int = 60,
) -> Dict[str, Any]:
    """Run Phase 3: Consumer Signal (C_t Computation)."""
    return get_orchestrator().run_consumer_signal(tickers, lookback_days)


def run_orchestration_cycle(portfolio_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run Full Orchestration Cycle."""
    return get_orchestrator().run_orchestration_cycle(portfolio_state)


def run_exit_engine(
    tickers: List[str],
    current_prices: Dict[str, float],
) -> Dict[str, Any]:
    """Run Stage 3: Exit Engine."""
    return get_orchestrator().run_exit_engine(tickers, current_prices)


def run_sizing_rebalancing(
    portfolio_state: Dict[str, Any],
    dcf_results: Dict[str, Any],
    entry_signals: Dict[str, Any],
    exit_signals: Dict[str, Any],
    black_swan_decisions: Dict[str, Any],
    fallback_weights: Dict[str, float],
    regime: str,
    hmm_crisis_prob: float,
    current_prices: Dict[str, float],
) -> Dict[str, Any]:
    """Run Stage 4: Sizing & Rebalancing."""
    return get_orchestrator().run_sizing_rebalancing(
        portfolio_state, dcf_results, entry_signals, exit_signals,
        black_swan_decisions, fallback_weights, regime, hmm_crisis_prob, current_prices
    )


def run_backtest(
    start_date: str,
    end_date: str,
    universe: Optional[List[str]] = None,
    initial_capital: float = 1_000_000,
) -> Dict[str, Any]:
    """Run Backtest Harness."""
    return get_orchestrator().run_backtest(start_date, end_date, universe, initial_capital)


# Startup hook â€” call this at the very top of any entry point
def initialize_agent_system(experiment_id: str = "sentiment-cascade-value-v3") -> AgentOrchestrator:
    """
    Call this ONCE at application startup to initialize the full agent system.
    
    Usage in any entry point (main.py, CLI, notebook, test runner):
        from agent_orchestrator import initialize_agent_system
        orchestrator = initialize_agent_system()
        # Now all logging carries experiment context, all agents registered
    """
    global _orchestrator
    _orchestrator = AgentOrchestrator(experiment_id)
    _orchestrator.startup()
    return _orchestrator


if __name__ == "__main__":
    # Demo: initialize and show routing
    orch = initialize_agent_system()
    
    # Example routing
    routing = orch.route_task(
        task_type="cdxj_harvest",
        payload={"tickers": ["AAPL", "MSFT"], "date_range": "2021-2026"},
        priority="high"
    )
    print(json.dumps(routing, indent=2, default=str))
