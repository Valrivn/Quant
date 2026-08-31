import pytest
from unittest.mock import patch
from psychological.qualitative_scoring import MoatComposite, _moat_scoring_config
from discovery.gate_data import qualitative_signals, _MOAT_KEYS
from config import load_hybrid_config

def test_moat_applicability_exclusion():
    # Verify for an excluded ticker, developer_momentum is ignored in MoatComposite
    mc = MoatComposite(ticker="TSM")
    mc.add_signal("product_breadth", 0.8)
    mc.add_signal("developer_momentum", 0.7)
    mc.add_signal("employee_sentiment", 0.6)
    
    assert "product_breadth" in mc.scores
    assert "employee_sentiment" in mc.scores
    assert "developer_momentum" not in mc.scores # Excluded!
    
    mc.compute_raw_composite()
    assert mc.n_signals == 2
    
    # Verify gate_data qualitative_signals returns developer_momentum as not_applicable for excluded tickers
    with patch("discovery.gate_data.get_connection") as mock_conn:
        signals, prov = qualitative_signals("TSM")
        assert set(signals.keys()) == set(_MOAT_KEYS)
        assert prov["developer_momentum"] == "not_applicable"

def test_single_factor_weight_cap():
    # Verify 40% single-factor cap of total contributing weight
    mc = MoatComposite(ticker="NVDA")
    mc.add_signal("product_breadth", 1.0) # config weight 0.30
    mc.add_signal("developer_momentum", 0.5) # config weight 0.20
    mc.add_signal("employee_sentiment", 0.5) # config weight 0.25
    mc.add_signal("network_effect_proxy", 0.5) # config weight 0.15
    mc.add_signal("revenue_concentration", 0.5) # config weight 0.05
    mc.add_signal("regulatory_barrier", 0.5) # config weight 0.05
    
    mc.compute_raw_composite()
    
    # Total contributing weight is sum of active weights
    total_active_w = sum(mc.weights.values())
    for k, w in mc.weights.items():
        assert w / total_active_w <= 0.4001 # Allowing tiny float precision margin

def test_trustworthiness_weights_marginal_shift():
    # Verify that changing weights in config shifts the composite in the expected direction
    cfg = load_hybrid_config()
    
    # Setup two MoatComposites with same signals but different weights in mock config
    mc_default = MoatComposite(ticker="NVDA")
    mc_default.add_signal("product_breadth", 1.0)
    mc_default.add_signal("developer_momentum", 0.2)
    mc_default.add_signal("employee_sentiment", 0.2)
    mc_default.add_signal("network_effect_proxy", 0.2)
    mc_default.compute_raw_composite()
    
    custom_moat_cfg = {
        "weights": {
            "product_breadth": 0.40,
            "developer_momentum": 0.20,
            "employee_sentiment": 0.20,
            "network_effect_proxy": 0.20,
            "revenue_concentration": 0.0,
            "regulatory_barrier": 0.0
        },
        "applicability": {
            "developer_momentum": {
                "excluded_tickers": ["TSM", "MU", "AVGO", "DELL", "SMCI"]
            }
        }
    }
    
    # Patch load_hybrid_config in qualitative_scoring where MoatComposite resides.
    # _moat_scoring_config is lru_cached, so clear it so the mocked config is picked up.
    _moat_scoring_config.cache_clear()
    try:
        with patch("psychological.qualitative_scoring.load_hybrid_config") as mock_load:
            merged = dict(cfg)
            merged["qualitative_moat_scoring"] = custom_moat_cfg
            mock_load.return_value = merged
            
            mc_custom = MoatComposite(ticker="NVDA")
            mc_custom.add_signal("product_breadth", 1.0)
            mc_custom.add_signal("developer_momentum", 0.2)
            mc_custom.add_signal("employee_sentiment", 0.2)
            mc_custom.add_signal("network_effect_proxy", 0.2)
            mc_custom.compute_raw_composite()
    finally:
        _moat_scoring_config.cache_clear()  # restore for subsequent tests
        
    # Since product_breadth (1.0) has higher weight in custom config, mc_custom composite should be higher than mc_default
    assert mc_custom.raw_composite > mc_default.raw_composite

def test_default_behavior_renormalization():
    # Verify default behavior (no excluded factors) still computes a valid composite renormalized to [0,1]
    mc = MoatComposite(ticker="NVDA")
    mc.add_signal("product_breadth", 0.9)
    mc.add_signal("developer_momentum", 0.8)
    mc.add_signal("employee_sentiment", 0.7)
    val = mc.compute_raw_composite()
    assert 0.0 <= val <= 1.0
