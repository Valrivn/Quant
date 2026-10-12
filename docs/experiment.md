# Experiment Design — Consumer→Trader→Street Cascade Timing a Long-Horizon Value Book
**Pre-registered 2026-09-05 rev 3 | Owner: big-pickle | Tier-3 — supersedes `data.md` (Data Remediation Plan).**
**Core hypothesis:** Edge = information synthesis from **public/common data** (consumer product reviews, adoption signals) on **neglected small caps** where Wall Street isn't looking, timing entry via a **three-layer cascade** (Consumer leads Trader leads Street) into a **quarterly-rebalanced value book** that beats SPY long-term with lower drawdowns. No latency/private info — just what's everywhere, read early and structured.

---

## 0. The Question (Causal Claim Being Tested)
> **Cascade:** Consumer/purchaser sentiment (actual product users, developers, B2B buyers) → Trader sentiment (retail forums, social) → Wall Street sentiment (analysts/institutions) → price.
> **Entry rule (Asymmetric Inversion):** `Buy iff C_t ↑ AND T_t ≈ 0 AND W_t ≈ 0` — product users enthusiastic, retail traders silent, analysts absent.
> **Universe filter (Street Neglect):** `Analysts ≤ 3 AND Inst Own < 35%` — only names Wall Street literally isn't covering.
> **Ultimate goal:** This strategy beats S&P 500 long-term (full window + per regime), with maxDD ≤ -25%, using only public/common data on under-followed names.

- **Null:** (a) no cascade ordering, OR (b) value book ≤ SPY, OR (c) drawdown breach.
- **Alternative:** cascade holds, value book beats SPY, survives regimes.
- **Falsifiers (pre-registered):**
  1. Consumer does NOT precede trader (ρ(C→T, k<0) ≤ 0.10) ⇒ cascade-gate unfounded.
  2. Value book total return/Sharpe ≤ SPY net of 50bps round-trip on full window.
  3. Portfolio maxDD > -25% in any crisis stratum (S1 dot-com, S5 COVID, S7 2022, S8 if turns).
  4. Street Neglect Filter universe yields < 20 names at any rebalance ⇒ discovery pool too thin.

---

## 1. STRATA — Pre-Registered Regime Windows (Fixed; No Window-Shopping)
| ID | Period | Regime | Layers Alive | Purpose |
|----|--------|--------|--------------|---------|
| S1 | 1999-03 → 2003-09 | **DOTCOM boom→bust** | CONSUMER only (no Trends) | value-leg validation: did quality survive bust? |
| S2 | 2008-01 → 2009-06 | **GFC bear** | consumer (news/trends) | value-through-fear calibration |
| S3 | 2010-01 → 2018-12 | neutral/bull mix | trader(2010)+consumer | earliest 3-layer testable window |
| S4 | 2019-01 → 2020-02 | bull | full | clean bull |
| S5 | 2020-03 → 2020-05 | COVID crash | full | crash stress |
| S6 | 2020-06 → 2021-12 | meme bull | full + ApeWisdom(2021) | hype regime; C vs T divergence test |
| S7 | 2022-01 → 2022-12 | rate-hike bear | full | .ai-era bear |
| S8 | 2023-01 → 2026-07 | **.ai boom bull** | all | the era you trade |

\* S1: no Google Trends (starts 2004). S1 validates VALUE leg only; cascade timing untestable — honest flag.

**Long horizon reality:** 252d returns over 8 years ≈ 8-10 non-overlapping obs/ticker. Headline test = strategy equity curve (monthly marks, Newey-West), not per-ticker IC alone.

**Scrape/ingest priority (maximizes regime coverage per effort):**
1. **2021→2026** (S6-S8): all layers incl. ApeWisdom + Google Trends; bull+bear+bull.
2. **2010→2020** (S3-S5): trader + consumer via CDXJ + Trends.
3. **S1/S2**: value-leg only (prices, SEC, FF5) — no sentiment needed.

---

## 2. UNIVERSE — Hard Street Neglect Filter (Applied BEFORE Any Signal Calc)
At each quarterly rebalance, the eligible universe = `t_pit_universe` ∩ **neglected names**:
```
N_analysts(ticker, as_of) ≤ 3     [from Kaggle analyst data]
AND
InstOwn_pct(ticker, as_of) < 35%  [from EDGAR 13F, aggregated]
```
- Sourced from: Kaggle analyst coverage counts + EDGAR 13F aggregated ownership.
- Applied in `t_pit_universe` construction — a hard gate, not a soft weight.
- If universe < 20 names at rebalance ⇒ ABSTAIN (never impute).

---

## 3. VARIABLES — Three Sentiment Layers Strictly Quarantined + Value Selection

### Layer 1 — CONSUMER / Product-Utility Sentiment (LEADING, Public/Common Data)
| Var | Construct (What It Measures) | Source | PIT Anchor | Tier |
|-----|------------------------------|--------|------------|------|
| C_GT | search-interest (adoption/curiosity) | **Google Trends (2004→, free)** — verified via pytrends/BigQuery | week end | GREEN |
| C_NW | brand/product-mention tone | GDELT (brand-domain filtered, NOT market-tone) | doc date | GREEN |
| C_REV | **product utility**: B2B reviews (G2/Capterra), App Store stars, dev commit adoption (GH Archive stars/forks) | Wayback CDXJ + Common Crawl (ragged) + GH Archive | capture ts | GREEN/YELLOW |
| C_TX | demand language on earnings calls (mgmt citing customer pull) | HF transcripts (kurry/glopardo) | call date | YELLOW |

> **Quarantine rule:** Reddit/StockTwits/ApeWisdom **never** enter Layer 1. They are *speculation*, not product utility. Mixing them re-introduces the retail hype trap.

### Layer 2 — TRADER / Retail-Speculation Sentiment (SECOND Layer)
| Var | Construct | Source | PIT Anchor | Tier |
|-----|-----------|--------|------------|------|
| T_AW | ApeWisdom mentions/hype | `apewisdom_history` | wayback capture | GREEN |
| T_ST | StockTwits sentiment | CDXJ harvest | capture ts | GREEN |
| T_RD | Reddit trading-sub composite | arctic_shift (sprint 2) | post UTC | YELLOW |
| T_IG | Instagram trend signal | existing telemetry | capture ts | YELLOW |

### Layer 3 — WALL STREET / Institutional Sentiment (THIRD Layer, The One to BEAT)
| Var | Construct | Source | PIT Anchor | Tier |
|-----|-----------|--------|------------|------|
| W_AN | analyst rating-action direction | Kaggle analyst accuracy | action date | GREEN |
| W_13F | institutional ownership change | EDGAR 13F direct | filed date (45d lag) | GREEN |
| W_TX | management-tone (street-facing guidance) | HF transcripts | call date | YELLOW |

### Value-Selection Inputs (Portfolio Engine — Distinct from Sentiment Timing)
| Var | Construct | Source | PIT Anchor |
|-----|-----------|--------|------------|
| V_MOAT | MoatComposite: product breadth, patents, network effect, rev concentration | SEC companyfacts + PatentsView + GDELT brand | as-of per source |
| V_QLTY | CultureComposite: employee/product quality proxy | pit_rating_snapshots + CDXJ web history | capture ts |
| V_VAL | earnings quality / DCF floor / R&D intensity / valuation (P/E, EV/EBITDA) | SEC companyfacts | filed date |

### Dependent Variables / Outcomes
- **DV-1 (cascade):** cross-layer lead-lag ρ(C_t, T_{t+k}), ρ(T_t, W_{t+k}), k∈{-21..+21}. Test: ρ(C→T, k<0) > 0.10, ρ(T→W, k<0) > 0.10.
- **DV-2 (ultimate):** strategy equity curve total return + Sharpe vs SPY (horizons 63/126/252), full window AND per stratum, net of 50bps round-trip.

### Control Variables (Separate Tables, PIT-Anchored)
| Class | Variables | Source |
|-------|-----------|--------|
| Factor loadings | MKT-RF, SMB, **HML (value!)**, RMW, CMA, MOM-check | Ken French daily |
| Valuation | P/E, P/B, EV/EBITDA (PIT) | SEC companyfacts |
| Market regime | bull/bear/neutral (VIX quantiles + 200d DD) | from prices |
| Size/liquidity | mkt-cap bucket, vol percentile, turnover | from prices |
| Macro/credit | BAA-OAS, 10y yield | FRED |
| Survival | delisted/merged flag | PIT universe |

---

## 4. TABLE SEPARATION — Anti-Lookahead Architecture (No Merged Storage)
**Rule:** No table holds both a feature and the DV. Joins ONLY at backtest runtime via `as_of` pivot (t+1 fill, engine-verified).

| Layer | Tables | Contents |
|-------|--------|----------|
| L1 source (append-only) | `t_source_trends`, `t_source_gdelt`, `t_source_reviews`, `t_source_stocktwits`, `t_source_reddit`, `t_source_apewisdom`, `t_source_analyst`, `t_source_13f`, `t_source_transcripts`, `t_source_secfacts`, `t_source_prices` | raw + TRUE `as_of` |
| L2 feature | `t_feature_consumer_*`, `t_feature_trader_*`, `t_feature_street_*`, `t_feature_value_*` | per-layer processed scores |
| L3 controls | `t_control_factors`, `t_control_regime`, `t_control_macro`, `t_control_valuation`, `t_control_style` | category-separated |
| L4 label | `t_label_forward` (DV-2) + `t_label_cascade` (DV-1 ρ inputs) | DV only |
| L5 PIT universe | `t_pit_universe` | membership ∩ neglect filter at quarter-end |

**Verifier (must pass pre-run):**
1. No L2 schema contains a DV column.
2. Each L1 row's `as_of` ≤ its capture/creation time (provenance check).
3. One `signal_provenance` row per (ticker, date, source) — unprovenanced = no backtest.
4. Fill date = `as_of` + 1 trade day (no same-close).

---

## 5. ENTRY / EXIT RULES (Frozen — The Asymmetric Inversion)

### Entry (All Must Hold at Rebalance Date t)
| Gate | Condition | Source |
|------|-----------|--------|
| **Neglect** | N_analysts ≤ 3 AND InstOwn < 35% | Kaggle + 13F |
| **Value** | V_VAL (DCF floor > price) AND V_MOAT > median cohort | SEC + PatentsView |
| **Consumer lead** | C_composite_t > C_composite_{t-1} (rising) | Layer 1 |
| **Trader silent** | T_composite_t ≤ 10th pctl of rolling 1y | Layer 2 |
| **Street absent** | W_AN_t = no upgrade in 90d AND W_13F_t flat | Layer 3 |

### Position Sizing / Portfolio
- Max 20 names, equal-weight within cohort, quarterly rebalance.
- Non-equity reserve (bonds/bills/gold) sized to keep portfolio maxDD ≤ -25% — enforced by `four_lane_pipeline` risk overlay.
- Round-trip cost = 50bps applied to every rebalance.

### Exit
- Any entry gate fails at rebalance → position closed t+1.
- Hard stop: portfolio drawdown > -25% ⇒ systematic de-risk to reserves.

---

## 5. SUCCESS / FAILURE BARS (Pre-Registered, Frozen — Rev 3)
- **C1 cascade:** ρ(C→T, k<0) > 0.10 AND ρ(T→W, k<0) > 0.10 on real data (neglected cohort).
- **C2 value leg:** V_MOAT/V_VAL composite IC > 0 at 126/252d horizon net of HML.
- **U1 ULTIMATE:** full-window strategy total return AND Sharpe > SPY (equity curve, net 50bps).
- **U2 per-regime:** beats SPY in ≥3 of {S4 bull, S6 meme, S7 bear, S8 .ai} with maxDD ≤ -25% in each.
- **Universe health:** ≥ 20 neglected names at ≥ 90% of rebalances.
- **S-bars:** `bayes_posteriors`, `signal_provenance`, `composite_scores` populated every rebalance; spot <1 capture/month ⇒ ABSTAIN.
- **REJECT if:** C1 fails OR U1 fails OR any single-source admission fails monotone-IC improvement.

---

## 6. ADMISSION TIERS (Layer-Mapped from Debrief B-20260905-001)
- **Green (sprint 1):** Google Trends (C_GT), GDELT brand (C_NW), GH Archive (C_REV-dev), CDXJ (T_ST, T_AW, C_REV-app), EDGAR companyfacts+13F (W_13F, V_VAL), analyst Kaggle (W_AN), Ken French (HML control), yfinance (prices/DV).
- **Yellow (sprint 2, gated by monotone-IC):** arctic_shift (T_RD), Common Crawl (C_REV depth), HF transcripts (C_TX, W_TX), PatentsView (V_MOAT).
- **Red (never):** live Glassdoor/Comparably/G2 scraping; data.world (dead); job-posting history.

---

## 7. REBALANCE CADENCE
- **Value book:** quarterly, 252d horizon (value patience, low turnover).
- **Cascade weights:** monthly recalibration (tunes WHEN, not the book's hands).
- **Frozen config:** no weight changes until a backtest justifies them (CEO only).

---

## 8. FILES WHEN APPROVED
- `db/schema*`: `t_source_*`, `t_feature_{consumer,trader,street,value}_*`, `t_control_*`, `t_label_*`, `t_pit_universe` with neglect filter.
- Backtest wiring: reuse `four_lane_pipeline` as_of read; populate `bayes_posteriors`/`signal_provenance`/`composite_scores` per rebalance.

---

## 9. AUDIT CHECKLIST (What You Verify Before Any Run)
| Check | How to Verify |
|-------|---------------|
| Universe is neglected | `t_pit_universe` shows N_analysts ≤ 3 & InstOwn < 35% at each quarter-end |
| Consumer ≠ Trader | `t_feature_consumer_*` never contains StockTwits/Reddit/ApeWisdom |
| Asymmetric Inversion coded | Entry logic enforces C↑ ∧ T≈0 ∧ W≈0 simultaneously |
| No lookahead | Grep DV from L2 schemas → zero hits; fill = as_of + 1 trade day |
| Costs real | 50bps round-trip applied to every rebalance in equity curve |
| Regime survival | Strategy maxDD ≤ -25% in S1, S5, S7 (tested explicitly) |

---

## 10. CHANGELOG FROM `data.md` (2026-09-03)
| Aspect | Old (`data.md`) | New (`experiment.md` rev 3) |
|--------|-----------------|----------------------------|
| Framing | Data remediation (fix broken feeds) | Pre-registered experiment (test specific hypothesis) |
| Goal | Fix data feeds so algorithms work | Test: public data on neglected names → value beats SPY |
| PIT Universe | S&P 500 reconstitution (P1 done) | Street Neglect Filter (analysts≤3, InstOwn<35%) |
| Sentiment | Unvalidated, lumped together | 3 quarantined layers (Consumer/Trader/Street) |
| Value | Implicit in MINVAR/MACRO | Explicit value book: DCF floor + Moat + quarterly 252d |
| Success Metric | Feed health gates | C1 cascade, C2 value, U1 vs SPY, U2 per-regime |
| Risk | Data quality | Regime drawdown ≤ -25%, universe ≥20 names |
| Data Sources | 6 priority gaps | 8 Green sources (sprint 1), 4 Yellow, 3 Red |
| Lookahead | Implicit | Explicit 5-layer table separation + verifier |

---

*This document is the single source of truth for the experiment. All pipeline work must reference these pre-registered parameters. Any change requires a new revision with CEO approval.*