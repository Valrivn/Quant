# Lane Alpha Conviction Scores — Actionable 0–10 Ratings
**Timestamp:** 2026-07-01T02:12:17Z
**Data Source:** `historical_5y_slice` (2021-06-30 to 2025-06-30)
**Configuration:** Pure Probabilistic Decoupled Parameter Mapping (No weights optimization)
## Conviction Scale
| Score | Interpretation |
|-------|---------------|
| 8–10 | **Strong Buy Right Now** |
| 6–7  | **Buy** |
| 4–5  | **Hold** |
| 2–3  | **Reduce** |
| 0–1  | **Don't Consider** |
## Rankings
| # | Ticker | Sector | Conviction | Label | Quality | Financial | Trajectory | Momentum | P(EVA > 0 at t=5) | P(ROIC > WACC) |
|---|--------|--------|-----------|-------|---------|-----------|------------|----------|-------------------|----------------|
| 1 | NVDA | semiconductors | **10/10** | Strong Buy | 0.858 | 0.854 | 0.931 | 0.641 | 100.00% | 100.00% |
| 2 | AMD | semiconductors | **10/10** | Strong Buy | 0.760 | 0.604 | 0.825 | 0.527 | 100.00% | 100.00% |
| 3 | AVGO | semiconductors | **10/10** | Strong Buy | 0.711 | 0.907 | 0.852 | 0.536 | 100.00% | 100.00% |
| 4 | INTC | semiconductors | **10/10** | Strong Buy | 0.354 | 0.384 | 0.512 | 0.371 | 100.00% | 100.00% |
| 5 | MSFT | platform_software | **10/10** | Strong Buy | 0.832 | 0.853 | 0.931 | 0.527 | 100.00% | 100.00% |
| 6 | GOOGL | platform_software | **10/10** | Strong Buy | 0.802 | 0.767 | 0.909 | 0.520 | 100.00% | 100.00% |
| 7 | META | platform_software | **10/10** | Strong Buy | 0.786 | 0.558 | 0.886 | 0.541 | 100.00% | 100.00% |
| 8 | TSLA | hardware_oem | **10/10** | Strong Buy | 0.679 | 0.805 | 0.750 | 0.532 | 100.00% | 100.00% |
| 9 | AAPL | hardware_oem | **10/10** | Strong Buy | 0.832 | 0.893 | 0.921 | 0.511 | 100.00% | 100.00% |
| 10 | AMZN | hardware_oem | **10/10** | Strong Buy | 0.794 | 0.776 | 0.905 | 0.502 | 100.00% | 100.00% |
## Component Breakdown
| Ticker | Conviction | Qual Z | Traj Signal | SBC Score | RD Eff | Momentum | Price | FCF (2025) |
| NVDA | 10/10 | 0.858 | sustainable | 0.583 | 1.000 | 0.641 | $130.0 | $45,000,000,000.0 |
| AMD | 10/10 | 0.760 | sustainable | 0.414 | 0.720 | 0.527 | $140.0 | $2,200,000,000.0 |
| AVGO | 10/10 | 0.711 | sustainable | 0.733 | 1.000 | 0.536 | $175.0 | $22,000,000,000.0 |
| INTC | 10/10 | 0.354 | undervalue | 0.404 | 0.594 | 0.371 | $22.0 | $-8,000,000,000.0 |
| MSFT | 10/10 | 0.832 | sustainable | 0.579 | 1.000 | 0.527 | $450.0 | $82,000,000,000.0 |
| GOOGL | 10/10 | 0.802 | sustainable | 0.333 | 1.000 | 0.520 | $185.0 | $86,000,000,000.0 |
| META | 10/10 | 0.786 | sustainable | 0.027 | 0.710 | 0.541 | $520.0 | $62,000,000,000.0 |
| TSLA | 10/10 | 0.679 | sustainable | 0.773 | 1.000 | 0.532 | $220.0 | $5,000,000,000.0 |
| AAPL | 10/10 | 0.832 | sustainable | 0.695 | 1.000 | 0.511 | $225.0 | $115,000,000,000.0 |
| AMZN | 10/10 | 0.794 | sustainable | 0.588 | 1.000 | 0.502 | $195.0 | $64,000,000,000.0 |
## Methodology

Conviction score is a 10,000-pass Monte Carlo simulation of four independent pillars:

1. **Expected Revenue Growth Rate (g)** modulated endogenously: g = Reinvestment Rate * ROIC
2. **Competitive Advantage Period (N_CAP)** scaled via MoatComposite trend mapping
3. **Discount Rate Adjustments** scaled via Culture and interest coverage bond rating
4. **Operating Margin Volatility** scaled via network concentration indices

## Architectural Constraints

- Flat Data Structure: Each pillar computed independently
- Deterministic Lower Bounds: All math via scaled tanh
- Absolute Temporal Alignment: Max date 2025-06-30; zero 2026 lookahead
- Zero Hardcoded Values: All distributions derived from historical slice data
