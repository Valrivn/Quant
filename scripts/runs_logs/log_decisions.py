from config.logging_config import college_event

decisions = {
    'project': 'sentiment-cascade-value-v3',
    'phases_completed': ['phase1_infrastructure', 'phase2_pipeline', 'phase3_integration'],
    'architectural_decisions': {
        'position_sizing': {
            'decision': 'Dynamic Monte Carlo calibration instead of static 5%/10% caps',
            'details': 'Company-specific entry at Price <= IV_P25 (25th percentile of 10,000-pass MC DCF). Sector discount = max(0.08, 1.2 * sigma_sector). Test via ANOVA, Welch t-test, Chi-square.',
            'config': 'config/position_sizing.yaml'
        },
        'fallback_assets': {
            'decision': 'Configurable via etf_config.yaml, physical gold trusts only',
            'details': '60% short-term Treasuries (BIL/SHV primary) + 40% physical gold trusts (GLDM/IAU). No miners (GDX). NAV clamping gate: freeze if discount > -50bps. Liquidity gate: ADV > 1M shares, spread <= 2bps.',
            'config': 'config/etf_config.yaml'
        },
        'black_swan_trigger': {
            'decision': 'Asymmetric Fast-Slow Gate (OR logic with tiered response)',
            'details': 'FAST: 5-day drawdown > 3.5σ -> Tier 1 Provisional (25% dry powder, IV_P25 entry, no HMM needed). SLOW: HMM P(Crisis) > 0.40 -> Tier 2 Structural (75-100% dry powder, IV_P10 entry). DISENGAGE: If HMM not CRISIS after 10 days -> cancel provisional.',
            'config': 'config/position_sizing.yaml (dry_powder tiers)'
        },
        'moat_verification': {
            'decision': 'C_t serves dual role - entry timing AND moat invariance',
            'details': 'Stage 2: C_t = Grassroots cascade velocity (Google Trends + App Reviews + GDELT + GitHub). Buy when C_t accelerating, T_t/W_t quiet. Black Swan: C_t >= mu_baseline while price crashes. If C_t collapses -> structural impairment, DO NOT BUY. Sector monitor: 4 layers (Supply chain K_c>=0.70, Practitioner OMR<0.20, Peer ICR Z<-1.0, Macro BAA>250bps).'
        },
        'pipeline_scope': {
            'decision': 'Phased build: Phase 1 (Stages 1-2 + Fallback + HMM), Phase 2 (Stages 3-4 + Backtest), Phase 3 (Alt data + Production backtest)',
            'details': 'Gemini Flash (Antigravity) capped at 200k tokens per phase. Nemotron finishes remaining.',
            'model': 'gemini_3_flash_antigravity'
        },
        'stage_definitions': {
            'stage1_dcf': 'Capitalized R&D, ROIC >= WACC+2%, Solvency (ICR>3x, OCF>0), Margin Health (Gross Margin trend >=0, Op Margin stability), Moat (OMR>=0.50)',
            'stage2_entry': 'Price <= 0.90 * IV_Fair (IV_P25), Grassroots Lead (C_t>0, T_t/W_t quiet), Street Coverage <=3 analysts, Liquidity (ADV_30d >= $5M, spread <= 0.35%)',
            'stage3_exit': 'Price > 1.15 * IV_Fair, Market Hysteria (Hype P95 or T_t/W_t Swarm), Structural Break (Gross Margin decay >300bps YoY or EVA<=0)',
            'stage4_sizing': 'HMM Regime allocation (EXP 80-100%, SHOCK 40-60%, CRISIS 60/40 fallback), Conviction multipliers (High 1.5x, Med 1.0x, Low 0.5x), Hard caps (15% single, 30% sector), Rebalancing (2% bands, $1k min, 15bps cost)'
        },
        'success_bars': {
            'C1_cascade': 'Lead-lag rho(C->T, k<0) AND rho(T->W, k<0) > 0.1 on real data',
            'C2_value': 'V_MOAT/V_VAL composite IC > 0 at 126/252d net of HML',
            'U1_ultimate': 'Full-window strategy TR AND Sharpe > SPY (equity curve)',
            'U2_regime': 'Beats SPY in >=3 of {S4,S6,S7,S8}, no >-25% maxDD in any',
            'REJECT': 'C1 or U1 fails; OR any single-source admission fails monotone-IC improvement'
        },
        'strata_definition': {
            'S4': '1999-03..2003-09 dot-com boom->bust',
            'S5': '2008-01..2009-06 GFC bear',
            'S6': '2010-01..2018-12 neutral/bull mix',
            'S7': '2019-01..2020-02 bull',
            'S8': '2020-03..2020-05 COVID crash',
            'S9': '2020-06..2021-12 meme bull',
            'S10': '2022-01..2022-12 rate-hike bear',
            'S11': '2023-01..2026-07 .ai boom bull'
        },
        'data_sources': {
            'infrastructure_completed': ['HMM regime detector (3 states, pure NumPy Baum-Welch)', 'CIC blueprint extractor (253 modules, 0 failures)', 'Nemotron brain + sensory layer', 'College logger (JSONL audit trail)'],
            'sprint1_sources_implemented': ['GDELT (masterfilelist + export archives)', 'GH Archive (hourly JSON.gz)', 'EDGAR 13F (SEC submissions API)', 'Ken French FF5 (FTP zip -> parquet)', 'Google Trends (pytrends backfill)', 'Kaggle Analyst (kaggle CLI)'],
            'checkpoint_verification': 'All 9 sources show non-zero items_processed in data/checkpoints/sprint1/'
        },
        'technical_patterns': {
            'logging': 'College-audited: every decision emits college_event()',
            'degraded_registry': 'Missing data -> degraded flag, never hard stop',
            'checkpointing': 'JSON checkpoints in data/checkpoints/{stage}/ for resume',
            'pure_functions': 'Type hints, dataclasses, no global state',
            'unit_testable': 'Each module has if __name__ == "__main__": smoke test',
            'agent_orchestration': 'Nemotron brain routes to specialized agents (Gemini 3 Flash builder, sensory workers)'
        }
    },
    'timestamp': '2026-09-06T15:30:00Z'
}

college_event('architectural_decisions_complete', **decisions)
print('All architectural decisions logged to audit ledger')