# Lane Alpha Conviction Scores — Actionable 0–10 Ratings
**Timestamp:** 2026-06-29T16:41:46Z
**Data Source:** `historical_5y_slice` (2021-06-30 to 2025-06-30)
**Weight Set:** w_culture=0.0200, w_moat=0.5050, w_hype=0.4750
## Conviction Scale
| Score | Interpretation |
|-------|---------------|
| 8–10 | **Strong Buy Right Now** |
| 6–7  | **Buy** |
| 4–5  | **Hold** |
| 2–3  | **Reduce** |
| 0–1  | **Don't Consider** |
## Rankings
| # | Ticker | Sector | Conviction | Label | Quality | Financial | Trajectory | Momentum |
|---|--------|--------|-----------|-------|---------|-----------|------------|----------|
| 1 | NVDA | semiconductors | **8/10** | Strong Buy | 0.684 | 0.854 | 0.852 | 0.632 |
| 2 | AVGO | semiconductors | **7/10** | Buy | 0.620 | 0.907 | 0.814 | 0.531 |
| 3 | MSFT | platform_software | **7/10** | Buy | 0.669 | 0.853 | 0.860 | 0.521 |
| 4 | GOOGL | platform_software | **7/10** | Buy | 0.654 | 0.767 | 0.868 | 0.516 |
| 5 | META | platform_software | **7/10** | Buy | 0.650 | 0.558 | 0.870 | 0.537 |
| 6 | TSLA | hardware_oem | **7/10** | Buy | 0.609 | 0.805 | 0.810 | 0.530 |
| 7 | AAPL | hardware_oem | **7/10** | Buy | 0.662 | 0.893 | 0.864 | 0.508 |
| 8 | AMZN | hardware_oem | **7/10** | Buy | 0.655 | 0.776 | 0.868 | 0.497 |
| 9 | AMD | semiconductors | **6/10** | Buy | 0.624 | 0.604 | 0.817 | 0.520 |
| 10 | INTC | semiconductors | **3/10** | Reduce | 0.461 | 0.384 | 0.100 | 0.382 |
## Component Breakdown
| Ticker | Conviction | Qual Z | Traj Signal | SBC Score | RD Eff | Momentum | Price | FCF (2025) |
| NVDA | 8/10 | 0.684 | sustainable | 0.583 | 1.000 | 0.632 | $130.0 | $45,000,000,000.0 |
| AVGO | 7/10 | 0.620 | sustainable | 0.733 | 1.000 | 0.531 | $175.0 | $22,000,000,000.0 |
| MSFT | 7/10 | 0.669 | sustainable | 0.579 | 1.000 | 0.521 | $450.0 | $82,000,000,000.0 |
| GOOGL | 7/10 | 0.654 | sustainable | 0.333 | 1.000 | 0.516 | $185.0 | $86,000,000,000.0 |
| META | 7/10 | 0.650 | sustainable | 0.027 | 0.710 | 0.537 | $520.0 | $62,000,000,000.0 |
| TSLA | 7/10 | 0.609 | sustainable | 0.773 | 1.000 | 0.530 | $220.0 | $5,000,000,000.0 |
| AAPL | 7/10 | 0.662 | sustainable | 0.695 | 1.000 | 0.508 | $225.0 | $115,000,000,000.0 |
| AMZN | 7/10 | 0.655 | sustainable | 0.588 | 1.000 | 0.497 | $195.0 | $64,000,000,000.0 |
| AMD | 6/10 | 0.624 | sustainable | 0.414 | 0.720 | 0.520 | $140.0 | $2,200,000,000.0 |
| INTC | 3/10 | 0.461 | overextended | 0.404 | 0.594 | 0.382 | $22.0 | $-8,000,000,000.0 |
## Methodology

Conviction score is a weighted composite of four independent pillars:

1. **Quality Score (30%)** — tanh((w_c·z_c + w_m·z_m + w_h·z_h) / 2) → unit scale
2. **Trajectory Score (25%)** — corridor position inverted if signal ∈ {overextended, distressed}
3. **Financial Health (25%)** — blend of (1 - SBC_drag) and RD_efficiency
4. **Momentum (20%)** — YoY blended z-change, 3yr FCF growth, 3yr revenue growth

All inputs pass through `tanh_clamp(z, scale=2.0)` enforcing bounds (-1, 1).

## Architectural Constraints

- Flat Data Structure: Each pillar computed independently
- Deterministic Lower Bounds: All math via scaled tanh
- Absolute Temporal Alignment: Max date 2025-06-30; zero 2026 lookahead
- Zero Hardcoded Values: Weights from Spearman ρ optimization on historical slice
