import os
import yaml
from typing import Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "reddit_weights.yaml")
HYBRID_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "hybrid_config.yaml")
FINTECH_CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "fintech_credentials.yaml")
HYBRID_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "hybrid_weights.yaml")

DEFAULT_WEIGHTS = {
    "categories": {
        "macro_geopolitical": {"weight": 0.25},
        "fundamental_institutional": {"weight": 0.25},
        "tech_product": {"weight": 0.30},
        "retail_options": {"weight": 0.20},
    },
    "subreddits": {},
    "optimization": {"learning_rate": 0.01},
}


def load_weights() -> Dict[str, Any]:
    """Load category weights from YAML config with fallback."""
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_WEIGHTS)
    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config or dict(DEFAULT_WEIGHTS)


def load_hybrid_config() -> Dict[str, Any]:
    """Load hybrid architecture configuration."""
    if not os.path.exists(HYBRID_CONFIG_PATH):
        return {}
    with open(HYBRID_CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config or {}


def load_fintech_credentials() -> Dict[str, Any]:
    """Load fintech API credentials from YAML config."""
    if not os.path.exists(FINTECH_CREDENTIALS_PATH):
        return {}
    with open(FINTECH_CREDENTIALS_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config or {}


def load_hybrid_weights() -> Dict[str, Any]:
    """Load hybrid source weights and fusion parameters."""
    if not os.path.exists(HYBRID_WEIGHTS_PATH):
        return {}
    with open(HYBRID_WEIGHTS_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config or {}


# Pre-load configuration
_config = load_weights()

SUBREDDIT_TAXONOMY = _config.get('subreddits', {})
CATEGORY_WEIGHTS = {k: v.get('weight', 0.25) for k, v in _config.get('categories', {}).items()}
OPTIMIZATION_CONFIG = _config.get('optimization', {})

HYBRID_SOURCE_WEIGHTS = {}
HYBRID_CATEGORY_WEIGHTS = {}
HYBRID_SUBREDDIT_WEIGHTS = {}
HYBRID_FUSION_PARAMS = {}
HYBRID_OPTIMIZATION_CONFIG = {}
