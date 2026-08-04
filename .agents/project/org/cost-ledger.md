# Cost Ledger — Token Spend per Decision

**Maintained by:** logger. One row per model invocation within a decision.
This file answers the CEO's standing question: "is the debate paying for
itself?"

Format (from token-budget.md):

```text
| date | decision-id | tier | models | in-tok | out-tok | est-cost | ruling | notes |
```

## Ledger

| date | decision-id | tier | models | in-tok | out-tok | est-cost | ruling | notes |
|------|-------------|------|--------|--------|---------|----------|--------|-------|
| 2026-08-01 | D-20260801-001 | T2 | big-pickle, gemini-planner | 35k | 15k | $0.50 | APPROVE | T2 execution approval |
| 2026-08-01 | D-20260801-002 | T3 | big-pickle, gemini-planner, hermes-bridge | 60k | 20k | $1.50 | APPROVE | T3 demo/audit |
| 2026-08-01 | D-20260801-003 | T3 | big-pickle, gemini-planner, hermes-bridge | 60k | 20k | $1.50 | APPROVE | T3 stochastic dashboard plan |
| 2026-08-01 | D-20260801-004 | T3 | big-pickle, gemini-planner, hermes-bridge | 60k | 20k | $1.50 | APPROVE | T3 value-eval architecture ruling |
| 2026-08-02 | D-20260801-004 | T3 | gemini-flash-worker (6 builds) + big-pickle (custodian) + conductor + logger | 180k | 60k | $4.50 | APPROVE (executed) | D-004 P0-P5 all gates PASS, 6-phase execution |
| 2026-08-02 | D-20260802 | T3 | deepseek-worker (audit) + big-pickle (A) + gemini-planner (B) + hermes-bridge (synthesis) | 75k | 22k | $2.00 | PENDING | CEO deep-review: OOS sim run, council debate, data-validity audit |
| 2026-08-02 | D-20260802 | T3 | big-pickle (custodian) + gemini-flash-worker (drafting) + conductor + logger | 60k | 20k | $1.50 | APPROVE | D-20260802 plan drafting |
| 2026-08-02 | D-20260802 | T3 | big-pickle (custodian) + gemini-planner (debate) + hermes-bridge (synthesis) | 75k | 22k | $2.00 | APPROVE (FINAL) | D-20260802 final-ruling debate and synthesis |
| 2026-08-02 | D-20260802-002 | T3 | big-pickle (pilot+evidence) + CEO ruling | 50k | 15k | $1.25 | MODIFY | P3 pilot + ablation ran; exit overlay + discovery pilot failed pre-registered gates; CEO re-thesis to reinvestment rate + qualitative moat |
| 2026-08-02 | D-20260802-002 | T3 | big-pickle (implementation) + conductor + logger | 60k | 20k | $1.50 | APPROVE | Reinvestment-rate + moat thesis implementation with quality gate validation |
| 2026-08-02 | D-20260802-002 | T3 | big-pickle (custodian) + gemini-flash-worker (drafting) + conductor + logger | 60k | 20k | $1.50 | APPROVE | Implementation artifacts and test suite generation |
| 2026-08-03 | D-20260803-001 | T3 | big-pickle (Position A) + big-pickle (Position B, degradation-ladder Step 2) + hermes-bridge (synthesis) + logger | 75k | 22k | $2.00 | APPROVE | Bounded falsification-first build; gemini-planner subagent returned 3 empty runs; planner-lane reliability issue noted |
| 2026-08-03 | D-20260803-002 | T3 | big-pickle (Position A) + gemini-planner (Position B) + big-pickle (disagreement map) + hermes-bridge (synthesis) + logger | 75k | 22k | $2.00 | MODIFY | Two-sleeve portfolio + cost-aware dynamic allocation + $10k fee simulation; hybrid deployment with transaction-cost flooring and opportunistic liquidation |
| 2026-08-03 | D-20260803-002 | T3 | big-pickle ($10k fee_sim build+run) + logger (recording) | 45k | 15k | $1.50 | MODIFY (executed) | $10k fee/turnover sim ran: CHURN fees 43.8% of gains vs OPPORTUNISTIC 0.44% (15 trades); 60/40 cost-aware $110 fees; summed backtests $176,400; registry + reflections recorded |
| 2026-08-03 | D-20260803-003 | T3 | big-pickle (Phase-1 build: sleeves/macro_state/risk_minimizer/fee_sim3 + 16 tests + data-layer) + conductor + logger | 120k | 40k | $3.50 | APPROVE (Phase 1 executed) | Phase-1 multi-asset $10k sim: BASELINE SPY $31,700 +217%; MACRO $22,982 +130% Sharpe 1.38 maxDD -15%; MINVAR $24,719 +147% Sharpe 1.43 maxDD -14%; dividend-fee-coverage 53-71x (CEO hypothesis confirmed); multi-source via Nasdaq API cross-check (raw-level corr 1.000 on BIL/SGOV, >=0.92 elsewhere); FRED unreachable this run -> documented HYG/LQD price-proxy macro fallback; conductor PASS 857 passed/18 skipped/0 failed; audit memo _drafts/audit-B-20260803-003.md |
| 2026-08-03 | D-20260803-004 | T3 | big-pickle (A) + gemini-planner (B) + big-pickle (map) + hermes-bridge (synthesis) + logger | 75k | 22k | $2.00 | APPROVE (hybrid + floor) | Phase-2 greenlit: stable-dividend audit (SEC XBRL + price-yield cross-check, 5y window, >=3% yield floor, minimum-candidates floor + bills fallback), opportunistic engine (reused D-20260802-002 swap + bear-regime gate, equity sleeve), DIVIDEND variant in fee_sim3; both Phase-1 greenlight conditions met (conductor 857/18/0, coverage 53-71x recorded) |
| 2026-08-03 | D-20260803-004 | T3 | big-pickle (Phase-2 build: dividend_audit + opportunistic + datastore dividends + DIVIDEND variant + 25 tests + sim + auditor) + conductor + logger | 100k | 35k | $3.00 | APPROVE (Phase 2 executed) | Phase-2 sim: DIVIDEND (stable-div + opportunistic) $14,188 +42% Sharpe 1.33 maxDD -6%, 1 trade, coverage 56x; opportunistic z-gate fired 0x (falsification); fixed tz-aware dividend bug + partial-year false-cut bug; 102 decisions = 25 bills-fallback/77 basket (avg 5 names); SEC XBRL + FRED cross-checks degraded (unreachable); conductor PASS 881/18/0; audit memo _drafts/audit-B-20260803-004.md |
| 2026-08-03 | D-20260803-005 | T3 | big-pickle (posA + map) + gemini-planner (posB) + hermes-bridge (synthesis) + CEO ruling | 22k | 9k | $0.90 | MODIFY (static + static-after-ML + adaptive, risk-constrained) | Three-strategy risk-constrained ML allocator: STATIC-40/20/20/20 + STATIC-after-ML + ADAPTIVE (Sharpe-max objective, risky-weight penalty, hard <=30% maxDD at every point); all params pre-registered in config/weights_diversification.yaml; hybrid base adopted |
| 2026-08-03 | D-20260803-005 | T3 | big-pickle (Phase-3 build: allocator.py GD optimizer + fee_sim3 STATIC/OPP-ONLY/STATIC-after-ML/ADAPTIVE + config/weights_diversification.yaml + 19 tests + sim + auditor) + conductor + logger | 120k | 42k | $3.60 | EXECUTED (risk-constrained ML allocator) | Phase-3 sim: STATIC-after-ML Sharpe 1.16 maxDD -16% (OOS Sharpe 1.90) beats STATIC-40/20/20/20 0.94/-21%; 30% DD bound honored everywhere; dividends 1.9-2.0x SPY; profit-change gate fired 0/102 (falsification); opportunistic-only drags -11pts; conductor PASS 900/18/0; audit memo _drafts/audit-B-20260803-005.md |

## Monthly roll-up (data-scientist writes)

| Month | Decisions | Total in-tok | Total out-tok | Est cost (paid) | Avg/decision |
|-------|-----------|--------------|---------------|-----------------|--------------|
| 2026-08 | 18 | 1172k | 427k | $34.75 | $1.93 |
