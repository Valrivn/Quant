# Complete Architectural Context & Decision Register
**Project:** House of Quant — Phase 9 v7 Unified Regime  
**Date:** 2026-10-05  
**Status:** ACTIVE — Awaiting 7 User Architectural Decisions  
**Orchestrator Rule:** ALL architectural decisions require explicit USER approval (see `opencode.json:architectural_decision_rules`)

---

## 📍 CURRENT SYSTEM STATE (FROZEN & VALIDATED)

### Phase 9 v7 — Unified 3-Layer Regime Architecture
| Component | Status | Details |
|-----------|--------|---------|
| **Regime Engine** | ✅ FROZEN | BOCPD + IO-HMM + Micro-States (NOT 2-state HMM) |
| **Pre-registration** | ✅ SHA-256 VERIFIED | `c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685` |
| **Validation** | ✅ PASS | ARI 0.6650 on 4/4 folds (tuned), target ≥0.45 |
| **Gate Architecture** | ✅ FROZEN | Single Composite DSR (N=513), NO hierarchy, NO Holm-Bonferroni |
| **qual_weight** | ✅ 0.0 FROZEN | Arms B/C retired per CEO ruling 2026-09-27 |
| **Provenance** | ✅ COMPLETE | R1-R6 ALL PASS, `provenance_chain=True` |
| **Tests** | ✅ 156/156 PASS | 11 modules, target 150+ MET |

### Sprint 9 Completion (2026-10-04)
| Priority | Item | Status |
|----------|------|--------|
| P0 | Composite/DSR Coupling FIXED | ✅ Code matches pre-reg, coupling probe passes |
| P1 | Occupancy Diagnostics ADDED | ✅ Fold 4 = VALID_BULL_MARKET (88.8% EXPANSION) |
| P2 | MDE Logic INVERTED | ✅ Lookahead ceiling flags operational (Sharpe 0.15 = PLAUSIBLE) |
| P3 | Test Expansion | ✅ 156 tests across 11 modules |
| P4 | Trial Ledger Evaluated | ✅ 513/513 trials, `provenance_chain=True` |

### Critical Blocker: Data Pipeline Defect
**Finding:** `three_arm_runner.load_data_from_store()` uses sparse SEC filings (13-140 tickers/mo) vs 1000 target from `monthly_reconstitution`
- WACC = 0.08 constant, ICR = 5.0 constant, Sector = "Technology" constant
- Qual data only 2023 → Arm C collapses to Arm A (identical returns)
- **3-Arm Results (HONEST on broken data):**
  - Arm A: -1.24% / -0.13 Sharpe / -32.96% DD
  - Arm B: -4.85% / -0.49 Sharpe / -35.79% DD
  - Arm C: -1.24% / -0.13 Sharpe / -32.96% DD (IDENTICAL to A)

---

## 🏗️ ARCHITECTURAL SPECIFICATION: CASCADING REGIME-EMBARGO SYSTEM

### Step 1: Cascading Relationship (Regime First → Asset State Second)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         REGIMESTATE (AUTHORITATIVE)                         │
│  • Broad asset allocation: equity vs defensive (BIL/SHY/GLDM)              │
│  • Sets confirmation criteria per regime:                                   │
│      - EXPANSION: wider exit thresholds (ride momentum)                    │
│      - CHOPPY/CRISIS: tighter thresholds (protect capital)                 │
│  • Controls: current_equity_cap (applies to ALL positions)                 │
│  • Tracks: market_event_triggered, trigger_type, days_since_confirmation   │
│  • Tracks: embargo_state, embargo_days_remaining (portfolio-level)         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ (cascades down)
┌─────────────────────────────────────────────────────────────────────────────┐
│                       REENTRYEMBARGO (PER-ASSET)                            │
│  • Individual execution discipline: prevents panic whipsawing              │
│  • Per-ticker: embargo_remaining = 60 days (21 business days minimum)      │
│  • When stock sold → tagged with embargo timer                             │
│  • INTERDEPENDENCE:                                                        │
│      - Cannot clear embargo if RegimeState is CRISIS                       │
│      - When RegimeState enters EXPANSION → embargo countdown ticks down    │
│      - Only decrements when RegimeState.current_equity_cap == 1.0          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Step 2: High-Bar Override (Breaking the Cooldown)

**Condition:** All 4 gates must be TRUE simultaneously to override embargo cooldown.

| Gate | Condition | Threshold | Rationale |
|------|-----------|-----------|-----------|
| **1. Extreme Valuation Discount** | Z-valuation ≤ -1.0 OR price < 10th percentile of 10k-pass Monte Carlo DCF | Z ≤ -1.0 / P10 DCF | Prevents override on standard 3% dips; requires extreme historical discount |
| **2. Damodaran Moat Spread Intact** | ROIC - WACC ≥ 2.0% AND EVA Prob = 100% | Spread ≥ 2% / EVA=1.0 | Ensures buying quality at temporary discount, not value traps |
| **3. Macro Spread Stabilization** | ΔCreditSpread_21d ≤ 0.0 bps/day | Velocity ≤ 0.0 | Waits for credit deterioration to flatten before stepping in |
| **4. Balance Sheet Fortress** | ICR ≥ 5.0 | ICR ≥ 5.0 | Only firms with 5x interest coverage survive panic |

**Override Logic:**
```python
def can_asset_reenter(ticker: str, asset_data: Dict) -> bool:
    # Regime must be EXPANSION (full equity)
    if regime.current_equity_cap < 1.0: return False
    
    # ALL 4 gates must pass
    if not (asset_data['valuation_zscore'] <= -1.0 or asset_data['dcf_p10']): return False
    if not (asset_data['roic'] - asset_data['wacc'] >= 0.02 and asset_data['eva_prob'] >= 1.0): return False
    if not (asset_data['spread_velocity_21d'] <= 0.0): return False
    if not (asset_data['icr'] >= 5.0): return False
    
    return True  # Override allowed
```

---

## 🗂️ CURRENT FOLDER STRUCTURE

```
./
├── regime/                          # Core regime logic
│   ├── regime_state.py              # RegimeState (persisted JSON, authoritative)
│   ├── embargo.py                   # ReEntryEmbargo (dual truth — NEEDS FIX)
│   ├── market_events.py             # Persistence gate (3 triggers)
│   ├── derisking.py                 # Graded de-risking (70/50/40%)
│   └── __init__.py
├── portfolio/                       # Portfolio construction
│   ├── allocator.py                 # Vol targeting, weight optimization
│   ├── position_state.py            # PositionState (Parquet, dwell tracking)
│   └── __init__.py
├── ingestion/                       # Data pipelines
│   ├── sec_bulk_loader.py           # Companyfacts.zip parser
│   ├── factor_computer.py           # 12 PIT factors (ROIC, WACC, ICR, etc.)
│   ├── wacc_engine_v2.py            # WACC v2 (Hamada, Ω_stress, total_beta)
│   ├── fred_alfred.py               # FRED macro data
│   ├── harness/
│   │   ├── gate_engine.py           # Composite DSR + 7-component Arm A
│   │   ├── three_arm_runner.py      # 3-arm backtest (A/B/C)
│   │   └── prereg.py                # Pre-registration verification
│   └── persistence/
│       └── merged_universe.py       # PIT universe builder
├── config/                          # Frozen configs
│   ├── pre_registration_v7.yaml     # Phase 9 v7 (FROZEN, SHA-256 pinned)
│   ├── arm_a_refactor_prereg.yaml   # Arm A params (DRAFT)
│   ├── industry_beta.yaml           # Damodaran unlevered betas + ratings
│   ├── damodaran_erp.yaml           # ERP by year
│   └── gates_v2.yaml                # Sector-relative screen params
├── data/                            # Runtime data (gitignored)
├── validation/                      # Audits
├── commands/                        # CLI commands
│   └── sleep.py                     # Audit Ledger snapshot
├── logs/college/                    # College audit trail (JSONL)
├── .agents/project/org/             # Project org
│   ├── blueprint.md                 # Frozen architecture
│   └── decisions/                   # Decision log
├── IMPLEMENTATION_PLAN.md           # 8-phase implementation plan
└── opencode.json                    # Orchestrator config (updated with decision rules)
```

---

## 🔴 7 ARCHITECTURAL DECISIONS REQUIRING YOUR APPROVAL

### Decision 1: Config Namespace for Arm A Refactor
**Context:** `arm_a_refactor_prereg.yaml` exists as separate DRAFT file. CEO decision (2026-10-04) recommended "Extend v7 with arm_a namespace".
- **Option A:** Extend `pre_registration_v7.yaml` with `arm_a:` namespace (keeps single frozen pre-reg)
- **Option B:** Keep separate `arm_a_refactor_prereg.yaml` (clean separation, but two pre-regs)
- **My Recommendation:** **Option A** — Single source of truth, v7 SHA-256 remains pinned, arm_a params versioned within

### Decision 2: Embargo Authority — Single Source of Truth
**Context:** Dual truth between `ReEntryEmbargo` (in-memory state machine) and `RegimeState` (persisted mirror). CEO decision recommended "RegimeState authoritative".
- **Option A:** Make `RegimeState` fully authoritative — remove `ReEntryEmbargo` class, keep only per-ticker embargo data in `PositionState`
- **Option B:** Keep both, but `RegimeState` is mirror, `ReEntryEmbargo` is transition logic
- **My Recommendation:** **Option A** — Eliminates dual-truth bug, single state machine, simpler

### Decision 3: Damodaran Data Refresh Cadence
**Context:** Need unlevered betas, ICR→rating→spread tiers, ERP from NYU Stern.
- **Option A:** Monthly automated fetch from `https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datacurrent.html`
- **Option B:** Manual trigger via `scripts/refresh_damodaran.py` (controlled, auditable)
- **My Recommendation:** **Option B** — Manual trigger with checksum verification, monthly cadence enforced by process

### Decision 4: Valuation Z-Score Methodology
**Context:** Gate 1 requires "Z ≤ -1.0 vs trailing 5-year valuation mean" or "10th percentile of 10k-pass Monte Carlo DCF".
- **Option A:** Trailing 5-year mean/std from Monte Carlo DCF distribution (per spec)
- **Option B:** Simpler historical P/E or EV/EBITDA percentile (faster, less precise)
- **My Recommendation:** **Option A** — Matches pre-registered specification exactly

### Decision 5: EVA Probability = 100% Computation
**Context:** Gate 2 requires "EVA Prob = 100%" — meaning EVA > 0 in all Monte Carlo paths.
- **Option A:** Full 10,000-pass Monte Carlo DCF, count paths where EVA > 0, require 100%
- **Option B:** Analytical approximation (faster, but approximate)
- **My Recommendation:** **Option A** — "100%" is binary, approximation introduces ambiguity

### Decision 6: Defensive Asset Allocation — Add Gold (GLDM)
**Context:** Spec mentions "Treasury bills like BIL/SHY and gold GLDM". Current `cash_instruments: ["BIL", "SHY"]`.
- **Option A:** Add GLDM → `cash_instruments: ["BIL", "SHY", "GLDM"]` (3-way defensive split)
- **Option B:** Keep BIL/SHY only (simpler, pure duration hedge)
- **My Recommendation:** **Option A** — Gold provides inflation/crisis hedge per spec

### Decision 7: Rebalance Frequency
**Context:** Arm A plan specifies quarterly calendar rebalance as default.
- **Option A:** Quarterly (4/yr) — lower cost, slower reaction, per Arm A plan default
- **Option B:** Monthly (12/yr) — more responsive, higher turnover
- **My Recommendation:** **Option A** — Quarterly per pre-registered plan, 15% hysteresis deadband handles noise

---

## 📋 DECISION LOG FORMAT (for Audit Ledger)

Each ruling should be logged as:
```json
{
  "ts": "2026-10-05T...",
  "level": "INFO",
  "logger": "quant_audit",
  "message": "AUDIT_EVENT: ceo_ruling_<decision_id> | {decision: <id>, ruling: <choice>, rationale: <text>, severity: CRITICAL|HIGH}"
}
```

---

## 🚫 ORCHESTRATOR CONSTRAINTS (ENFORCED)

Per `opencode.json:architectural_decision_rules`:

| Forbidden | Required |
|-----------|----------|
| ❌ Modify pre-registration configs without user approval | ✅ Present options with trade-offs |
| ❌ Change component authority | ✅ Document recommendation |
| ❌ Add/remove data sources | ✅ Wait for explicit user ruling |
| ❌ Modify override gate conditions | ✅ Log ruling in Audit Ledger |
| ❌ Restructure folders/blueprints | ✅ Dispatch implementation after ruling |
| ❌ Assume defaults for unresolved questions | ✅ Never proceed without approval |

---

## 📄 KEY REFERENCE FILES

| File | Purpose |
|------|---------|
| `IMPLEMENTATION_PLAN.md` | 8-phase detailed plan with parallel dispatch |
| `Tomorrow.md` | Master resume, current status, next actions |
| `Data_Strategy.md` | Data strategy, dual-window design, gate specs |
| `.agents/project/org/blueprint.md` | Frozen v7 architecture |
| `config/pre_registration_v7.yaml` | Frozen pre-reg (SHA-256 pinned) |
| `config/arm_a_refactor_prereg.yaml` | Arm A refactor params (DRAFT) |
| `logs/college/2026-10-04/college.jsonl` | Sprint 9 CEO rulings |
| `logs/college/2026-10-05/college.jsonl` | Sleep command execution logs |

---

## 🎯 NEXT ACTIONS (BLOCKED ON 7 DECISIONS)

| Priority | Action | Blocked By |
|----------|--------|------------|
| **P0A** | Build merged PIT universe (reconstitution + fundamentals forward-fill) | Data pipeline fix (independent) |
| **P0B** | Fix composite_score definition | Decision 1 (config namespace) |
| **P1** | RegimeState authoritative rewrite | Decision 2 (embargo authority) |
| **P2** | High-bar override (4 gates) in gate_engine | Decisions 3,4,5 (data sources, valuation, EVA) |
| **P3** | SEC OCF/FCF loader | Decision 3 (Damodaran refresh) |
| **P4** | Damodaran loader + monthly refresh | Decision 3 |
| **P5** | Add GLDM to cash_instruments | Decision 6 |
| **P6** | Set quarterly rebalance | Decision 7 |

---

## 🔍 AUDIT FINDINGS: DEAD CODE & LOGIC ERRORS

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `portfolio/allocator.py` | 351-352 | Dead code after `return` in `portfolio_backtest()` | DELETE |
| `ingestion/harness/three_arm_runner.py` | 199-200 | Duplicate `apply_vol_targeting` after return | DELETE (use allocator version) |
| `regime/embargo.py` | 51-70 | Dual state machine, incorrect counter increments | REWRITE (delegate to RegimeState) |
| `ingestion/harness/gate_engine.py` | 30-49 | Two composite specs (ARM_A vs COMPONENT_SPECS) | UNIFY to ARM_A_COMPONENT_SPECS |
| `regime/regime_state.py` | 162-165 | `advance_day()` increments embargo counter when triggers_active | FIX: only decrement when inactive |

---

## ✅ VERIFICATION CHECKLIST (Pre-Dispatch)

Before parallel dispatch, verify:

- [ ] All 7 decisions ruled by user
- [ ] Rulings logged in Audit Ledger with timestamps
- [ ] `opencode.json` architectural_decision_rules enforced
- [ ] Implementation plan phases assigned to workstreams
- [ ] Dead code locations confirmed for deletion
- [ ] Blueprint folder structure documented
- [ ] Pre-registration v7 SHA-256 unchanged

---

**This document serves as the single source of truth for all architectural context. The orchestrator will reference this and `opencode.json` rules for every decision point.**