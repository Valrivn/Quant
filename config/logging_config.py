"""
Centralized logging configuration for the quant pipeline.

Provides structured logging with consistent formatting, levels, and handlers
across all modules. Supports both console and file output with rotation.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, Optional


# Default log format
DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DETAILED_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(funcName)s | %(message)s"


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    detailed: bool = False,
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Configure root logger with console and optional file handlers.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional path to log file
        detailed: Use detailed format with line numbers
        console: Enable console output
        max_bytes: Max size per log file before rotation
        backup_count: Number of rotated files to keep
    
    Returns:
        Configured root logger
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    formatter = logging.Formatter(DETAILED_FORMAT if detailed else DEFAULT_FORMAT)
    
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    
    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("nltk").setLevel(logging.WARNING)
    logging.getLogger("optuna").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("streamlit").setLevel(logging.WARNING)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a module-specific logger.
    
    Usage:
        logger = get_logger(__name__)
        logger.info("Message")
    """
    return logging.getLogger(name)


class LogContext:
    """Context manager for adding contextual information to log messages."""
    
    def __init__(self, logger: logging.Logger, **context):
        self.logger = logger
        self.context = context
        self.old_factory = logging.getLogRecordFactory()
    
    def __enter__(self):
        def record_factory(*args, **kwargs):
            record = self.old_factory(*args, **kwargs)
            for key, value in self.context.items():
                setattr(record, key, value)
            return record
        
        logging.setLogRecordFactory(record_factory)
        return self
    
    def __exit__(self, *args):
        logging.setLogRecordFactory(self.old_factory)


# Convenience function for quick setup
def init_logging(level: str = "INFO", log_dir: str = "logs", inject_experiment: bool = False) -> None:
    """Initialize logging with standard configuration."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    log_file = Path(log_dir) / "quant_pipeline.log"
    setup_logging(level=numeric_level, log_file=str(log_file), console=True)


def college_event(event: str, **kwargs) -> None:
    """College-audited event logging - emits structured event for audit trail."""
    logger = get_logger("college_audit")
    logger.info(f"COLLEGE_EVENT: {event} | {kwargs}")


def get_experiment_context() -> Dict[str, Any]:
    """Frozen experiment-design context injected into every agent payload.

    Restores the context source consumed by agent_orchestrator
    (`AgentOrchestrator._verify_experiment_context` hard-fails on mismatch
    and `_log_startup_banner` renders these keys). Values are sourced from
    the frozen design docs (startup/Data_Strategy.md sections 3 and 6;
    pre-registration v7 SHA-256 c80a47bf...):

        universe_filter     ~1,500 PIT names: top-1000 + next-500 mid/micro,
                           monthly reconstitution (RPO-clean 2018-2024)
        entry_gate          DCF value screen: ROIC > 10%, ICR > 3x, FCF > 0,
                           Price <= IV_P25 (sector-relative dual track)
        rebalance_cadence  monthly (data available at prior month-end)
        cost_bps           20 bps round-trip (10 bps per side) + slippage model
        max_drawdown       gate threshold 20% (Arm C max DD < -20%)
        tiers             3 arms: A pure quant / B pure qual / C hybrid
    """
    return {
        "experiment_id": "sentiment-cascade-value-v3",
        "version": "2026-09-05-rev3",
        "pre_registered": True,
        "universe_filter": "PIT top-1500 (top-1000 + next-500, monthly reconstitution)",
        "entry_gate": "DCF value: ROIC>10%, ICR>3x, FCF>0, Price<=IV_P25",
        "rebalance_cadence": "monthly",
        "cost_bps": 20,
        "max_drawdown": 0.20,
        "tiers": ["A_quant", "B_qual", "C_hybrid"],
    }


if __name__ == "__main__":
    # Demo
    init_logging("DEBUG")
    logger = get_logger(__name__)
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")