import os
import yaml
from typing import Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "reddit_weights.yaml")
HYBRID_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "hybrid_config.yaml")
FINTECH_CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "fintech_credentials.yaml")
HYBRID_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "hybrid_weights.yaml")


def load_weights() -> Dict[str, Any]:
    """Load subreddit and category weights from YAML config."""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config


def load_hybrid_config() -> Dict[str, Any]:
    """Load hybrid architecture configuration."""
    if not os.path.exists(HYBRID_CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {HYBRID_CONFIG_PATH}")
    with open(HYBRID_CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config


def load_fintech_credentials() -> Dict[str, Any]:
    """Load fintech API credentials from YAML config."""
    if not os.path.exists(FINTECH_CREDENTIALS_PATH):
        raise FileNotFoundError(f"Config file not found: {FINTECH_CREDENTIALS_PATH}")
    with open(FINTECH_CREDENTIALS_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config


def load_hybrid_weights() -> Dict[str, Any]:
    """Load hybrid source weights and fusion parameters."""
    if not os.path.exists(HYBRID_WEIGHTS_PATH):
        raise FileNotFoundError(f"Config file not found: {HYBRID_WEIGHTS_PATH}")
    with open(HYBRID_WEIGHTS_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config


_config = load_weights()
SUBREDDIT_TAXONOMY = _config["subreddit_weights"]
CATEGORY_WEIGHTS = _config["category_weights"]
OPTIMIZATION_CONFIG = _config.get("optimization", {})

_hybrid_config = load_hybrid_config()
HYBRID_SCENARIOS = _hybrid_config.get("scenarios", {})
HYBRID_FALLBACK = _hybrid_config.get("fallback", {})
HYBRID_FUSION = _hybrid_config.get("fusion", {})
HYBRID_OPTIMIZATION = _hybrid_config.get("optimization", {})
HYBRID_ENDPOINTS = _hybrid_config.get("endpoints", {})
HYBRID_RETRY = _hybrid_config.get("retry", {})

_hybrid_weights = load_hybrid_weights()
HYBRID_SOURCE_WEIGHTS = _hybrid_weights.get("source_weights", {})
HYBRID_CATEGORY_WEIGHTS = _hybrid_weights.get("category_weights", {})
HYBRID_SUBREDDIT_WEIGHTS = _hybrid_weights.get("subreddit_weights", {})
HYBRID_FUSION_PARAMS = _hybrid_weights.get("fusion", {})
HYBRID_OPTIMIZATION_CONFIG = _hybrid_weights.get("optimization", {})

_fintech_config = load_fintech_credentials()
FINTECH_CREDENTIALS = _fintech_config

__all__ = [
    "load_weights", "SUBREDDIT_TAXONOMY", "CATEGORY_WEIGHTS", "OPTIMIZATION_CONFIG",
    "load_hybrid_config", "HYBRID_SCENARIOS", "HYBRID_FALLBACK", "HYBRID_FUSION",
    "HYBRID_OPTIMIZATION", "HYBRID_ENDPOINTS", "HYBRID_RETRY",
    "load_fintech_credentials", "FINTECH_CREDENTIALS",
    "load_hybrid_weights", "HYBRID_SOURCE_WEIGHTS", "HYBRID_CATEGORY_WEIGHTS",
    "HYBRID_SUBREDDIT_WEIGHTS", "HYBRID_FUSION_PARAMS", "HYBRID_OPTIMIZATION_CONFIG"
]