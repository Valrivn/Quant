# P2 Verdict — Deterministic Ranker + Sanitizers + Concepts (D-20260806-001)

**Verdict artifact:** `trend-feed-p2-ranker-spec.md`
**Date:** 2026-08-07 (UTC)
**Branch:** `feature/b-20260806-001`
**Ruling:** D-20260806-001 (MODIFY) — falsification-first, deterministic, no RNG
**Kill-switch:** `discovery.enabled: false` — feed research-only, nothing wired to production.

## 1. Pre-registered ranker spec (SEC 3.4)

**Formula** (implemented in `discovery/ranker.py`):

```
trend_score(entity) = sum_src w_src * norm_rank_src
                    + w_vel * velocity_z
                    + w_cross * agreement_count
                    + w_topic * topic_relevance
                    - w_ad * ad_flag
                    - w_clout * clout_flag
```

- `norm_rank_src` = `1/rank` (rank is 1-based; first place = 1.0, second = 0.5, ...). Documented in config `ranker.norm_rank: "1/rank"`.
- `velocity_z` = `(mentions_7d - mentions_28d) / (mentions_28d + 1)` — deterministic, no cross-entity sampling.
- `agreement_count` = number of live independent sources flagging the entity.
- `topic_relevance` = `1 - (topic_priority_index / (n_topics - 1))` for topics in the priority list, else 0.
- `ad_flag` / `clout_flag` = sanitizer flags (0/1).

**Weights** (from `config/weights_discovery.yaml`, invariant 4 — nothing hard-coded):

| term | weight |
|---|---|
| sec_edgar_new_filers | 0.20 |
| reddit | 0.15 |
| stocktwits | 0.10 |
| apewisdom | 0.05 |
| w_vel | 0.20 |
| w_cross | 0.15 |
| w_topic | 0.15 |
| **positive sum** | **1.00** |
| w_ad (penalty) | 0.50 |
| w_clout (penalty) | 0.50 |

**Ordering:** score desc; tie-break by (topic priority index, lexicographic ticker). `top_k = 10` per cycle.

**Config validation (fail closed):** unknown keys, NaN, non-normalized positive weights (sum != 1.0), or missing required key => `DiscoveryConfigError` at load.

## 2. Determinism guarantees

- No `random`, `np.random`, `epsilon`, or stochastic draw anywhere in `discovery/` (CI grep-asserted by `tests/test_discovery_ranker.py::TestNoRngAudit`).
- Identical input -> identical output (tested).
- Deterministic tie-break (topic priority, then lexicographic ticker) (tested).
- Config completeness validated at load (tested).

## 3. Sanitizer rules (SEC 4, all thresholds from config)

**Clout-chaser** (`sanitizers.clout_chaser`): exclude when runup_ratio >= 0.85 AND mention_velocity >= 3.0 AND explosion_lag > 0 (spike LAGS run-up start). Reason codes: `runup_ratio>=floor`, `mention_velocity>=floor`, `spike_lags_runup`.

**Niche / minimal-popularity** (`sanitizers.niche`): views <= 100,000; comment-to-view in [0.001, 0.10]; view-to-follower in [0.5, 20.0].

**Ad / sponsored** (`sanitizers.ad`) — OR-rule, any hit => EXCLUDE:
- hashtags `#ad #sponsored #spon #partner` (case-insensitive),
- affiliate/tracked patterns `/ref=`, `bit.ly`, `linktr.ee`, `?utm_`, `shop.`,
- brand-account signals `verified_brand`, `bio_product_link`,
- engagement anomalies: comment-to-view outside [0, 0.25] OR view-to-follower outside [0, 50].

**Application mode** (`ranker.sanitizer_apply`): `ad: exclude` (hard exclusion), `clout: penalty` (folded into score).

## 4. Ecosystem / tandem extraction (SEC 4.2)

`discovery/ecosystem.py` provides a deterministic co-mention graph + a pluggable `classifier_hook` (read-only). When no hook is provided, a deterministic hub-and-spoke heuristic scores chain content and monopoly/dependency. **ASML fixture encoded** (CEO example): `ASML` co-mentioned with `NVDA/TSM/INTC/AMD` -> `monopoly_dependency=True`, `chain_score=0.8`.

## 5. Concept backlog schema (SEC 3.5)

`discovery/concepts.py`:
- `Concept`: `concept_id, concept_name, topic, first_seen, sources[], linked_tickers[] (informational), hypothesis (nullable), status`.
- `ConceptBacklog.add()` dedups by name, defaults status to `research_backlog`.
- `ConceptBacklog.to_candidate()` **raises `ConceptToCandidateError`** — a concept can never enter candidate/allocation lists.
- Deterministic ticker-vs-concept classifier: 1-5 uppercase letters (optionally `.X`) = ticker; else concept.

## 7. P2 verdict

**PASS.**

All P2 deliverables are implemented and tested: deterministic ranker (SEC 3.4 formula, config-driven, fail-closed validation), sanitizers (clout-chaser / niche / ad, all thresholds from config), ecosystem (deterministic + pluggable hook, ASML fixture), concepts (backlog + hard refusal to candidate path), and the new pre-registration `config/weights_discovery.yaml`. Kill-switch stays `false`; feed remains research-only.

**No blocker.** P2 gate (SEC 2.2) is satisfied: determinism proven, tie-break stable, config completeness validated, concept separation enforced.

## 8. Files

- `config/weights_discovery.yaml` (new, additive; existing weight configs untouched)
- `discovery/config_loader.py`, `discovery/ranker.py`, `discovery/sanitizers.py`, `discovery/ecosystem.py`, `discovery/concepts.py`
- `tests/test_discovery_ranker.py`, `tests/test_discovery_sanitizers.py`, `tests/test_discovery_concepts.py`

No commit made; this artifact is research-only.