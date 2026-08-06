DISAGREE-ON:
- Phase 5 (Markov Lynch states): B calls a 6-state matrix on ~8y of monthly data under-populated and noise-prone; A says it reuses the blueprint's existing lifecycle and a stability gate disables it if unstable. Real difference: whether Phase 5 should be built at all vs. scoped as a gated feasibility study.
- Phase 2 lambda: B warns the transaction penalty calibrated on the HYG/LQD proxy (FRED down) could be wrong for real regimes; A plans a sensitivity sweep 0.1-1.0. B wants the sweep mandatory before any penalty is trusted.
- Phase 3 Bayesian gate: B warns a rigid posterior threshold could freeze the allocator in minor regime shifts; A keeps thresholds in config but should add a no-trade band so a frozen state cannot persist.
CONSENSUS-ON:
- Approve the plan overall; Decision A -> rolling-3Y win-rate leg; Decision B -> Phase 0+2 first, re-baseline before stochastic layers; Decision C -> one ruling per phase.
- Double-descent capacity selection on train-only; MC must pass a calibration gate before surfacing; all new knobs in weights_diversification.yaml (invariant 4).
RESOLUTION-PATH:
- Scope Phase 5 as a feasibility study gated on transition-matrix population (min observations per cell, stability across eras) with hard disable if unmet.
- Make Phase 0 FRED-key/VCIT-SHY parity a PREREQUISITE to Phase 2 so lambda is not proxy-calibrated.
- Phase 3 thresholds get a pre-registered hold-band that forbids a permanently frozen allocator.
