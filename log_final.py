from config.logging_config import setup_college_file, college_event

# Setup once
handler = setup_college_file()
print('Handler:', handler)

# Log all remaining events
college_event('phase_3_integration_complete', 
    phase='phase3_integration',
    components=['consumer_signal.py', 'run_production_backtest.py', 'entry_timing.py_real_Ct', 'portfolio_orchestrator.py_Ct_integration', 'agent_orchestrator.py_consumer_signal_worker'],
    success_bars_validated=['C1_cascade', 'C2_value', 'U1_ultimate', 'U2_regime'],
    strata_covered=['S4', 'S5', 'S6', 'S7', 'S8'],
    alt_data_sources=6
)
print('Phase 3 logged')

college_event('architectural_decisions_complete', 
    project='sentiment-cascade-value-v3',
    phases_completed=['phase1_infrastructure', 'phase2_pipeline', 'phase3_integration'],
    architectural_decisions={
        'position_sizing': {'decision': 'Dynamic Monte Carlo calibration', 'config': 'config/position_sizing.yaml'},
        'fallback_assets': {'decision': 'Configurable physical gold trusts only (GLDM/IAU)', 'config': 'config/etf_config.yaml'},
        'black_swan_trigger': {'decision': 'Asymmetric Fast-Slow Gate (OR logic)', 'config': 'config/position_sizing.yaml'},
        'moat_verification': {'decision': 'C_t dual role: entry timing + moat invariance'},
        'pipeline_scope': {'decision': 'Phased build with Gemini Flash 200k token cap'},
        'stage_definitions': {'stage1_dcf': 'Capitalized R&D, ROIC>=WACC+2%, ICR>3x, Margin Health, OMR>=0.50', 'stage2_entry': 'P<=0.90*IV_Fair, C_t>0, T_t/W_t quiet, Coverage<=3, ADV>=5M', 'stage3_exit': 'P>1.15*Fair, Hysteria P95, Margin decay>300bps, EVA<=0', 'stage4_sizing': 'HMM regime alloc, Conviction multipliers, Caps 15%/30%, Rebalancing 2% bands'},
        'success_bars': {'C1': 'rho(C->T)>0.1 & rho(T->W)>0.1', 'C2': 'IC>0 net HML', 'U1': 'TR/Sharpe>SPY', 'U2': '>=3/4 strata wins, maxDD>-25%'},
        'strata': {'S4': '1999-2003 dot-com', 'S5': '2008-2009 GFC', 'S6': '2010-2018 neutral', 'S7': '2019-2020 bull', 'S8': '2020-2020 COVID', 'S9': '2020-2021 meme', 'S10': '2022 rate-hike', 'S11': '2023-2026 AI boom'},
        'data_sources': {'infrastructure': ['HMM', 'CIC', 'Nemotron brain', 'College logger'], 'sprint1': ['GDELT', 'GH Archive', 'EDGAR 13F', 'Ken French', 'Google Trends', 'Kaggle Analyst']},
        'technical_patterns': ['College-audited logging', 'Degraded registry', 'Checkpointing', 'Pure functions', 'Unit-testable', 'Agent orchestration']
    }
)
print('Architectural decisions logged')

import os
print('Lines:', sum(1 for _ in open('logs/college/2026-09-06/college.jsonl')))