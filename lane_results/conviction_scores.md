# Lane Alpha Conviction Scores — Actionable 0–10 Ratings
**Timestamp:** 2026-06-29T16:02:46Z**Data Source:** `historical_5y_slice` (2021-06-30 to 2025-06-30)**Weight Set:** w_culture=0.0250, w_moat=0.5000, w_hype=0.4750
## Conviction Scale
| Score | Interpretation ||-------|---------------|| 8–10 | **Strong Buy Right Now** || 6–7  | **Buy** || 4–5  | **Hold** || 2–3  | **Reduce** || 0–1  | **Don't Consider** |
## Rankings
| # | Ticker | Sector | Conviction | Label | Quality | Financial | Trajectory | Momentum ||---|--------|--------|-----------|-------|---------|-----------|------------|----------|| 1 | NVDA | semiconductors | **7/10** | Buy | 0.887 | 0.813 | 0.241 | 0.769 || 2 | AVGO | semiconductors | **7/10** | Buy | 0.743 | 0.902 | 0.354 | 0.591 || 3 | AMD | semiconductors | **6/10** | Buy | 0.753 | 0.660 | 0.357 | 0.501 || 4 | MSFT | platform_software | **6/10** | Buy | 0.851 | 0.821 | 0.233 | 0.557 || 5 | GOOGL | platform_software | **6/10** | Buy | 0.817 | 0.519 | 0.418 | 0.568 || 6 | META | platform_software | **6/10** | Buy | 0.808 | 0.494 | 0.383 | 0.678 || 7 | TSLA | hardware_oem | **6/10** | Buy | 0.721 | 0.909 | 0.388 | 0.518 || 8 | AAPL | platform_software | **6/10** | Buy | 0.835 | 0.797 | 0.231 | 0.511 || 9 | AMZN | platform_software | **6/10** | Buy | 0.820 | 0.589 | 0.412 | 0.681 || 10 | INTC | semiconductors | **3/10** | Reduce | 0.420 | 0.336 | 0.172 | 0.413 |
## Component Breakdown
| Ticker | Conviction | Qual Z | Traj Signal | SBC Drag | RD Eff | Momentum | Price | FCF (2025) ||--------|-----------|--------|-------------|----------|--------|----------|-------|------------|| NVDA | 7/10 | 0.887 | undervalue | 0.373 | 1.000 | 0.769 | $130 | $45,000,000,000 || AVGO | 7/10 | 0.743 | undervalue | 0.197 | 1.000 | 0.591 | $175 | $22,000,000,000 || AMD | 6/10 | 0.753 | undervalue | 0.400 | 0.720 | 0.501 | $140 | $2,200,000,000 || MSFT | 6/10 | 0.851 | undervalue | 0.358 | 1.000 | 0.557 | $450 | $82,000,000,000 || GOOGL | 6/10 | 0.817 | sustainable | 0.962 | 1.000 | 0.568 | $185 | $86,000,000,000 || META | 6/10 | 0.808 | undervalue | 0.722 | 0.710 | 0.678 | $520 | $62,000,000,000 || TSLA | 6/10 | 0.721 | undervalue | 0.182 | 1.000 | 0.518 | $220 | $5,000,000,000 || AAPL | 6/10 | 0.835 | undervalue | 0.405 | 1.000 | 0.511 | $225 | $115,000,000,000 || AMZN | 6/10 | 0.820 | sustainable | 0.821 | 1.000 | 0.681 | $195 | $64,000,000,000 || INTC | 3/10 | 0.420 | undervalue | 0.921 | 0.594 | 0.413 | $22 | $-8,000,000,000 |
## Methodology

Conviction score is a weighted composite of four independent pillars:

1. **Quality Score (30%)** — tanh((0.025·z_culture + 0.5·z_moat + 0.475·z_hype) / 2) → unit scale
2. **Trajectory Score (25%)** — corridor position inverted if signal ∈ {overextended, distressed}
3. **Financial Health (25%)** — blend of (1 - SBC_drag) and RD_efficiency
4. **Momentum (20%)** — YoY blended z-change, 3yr FCF growth, 3yr revenue growth

All inputs pass through `tanh_clamp(z, scale=2.0)` enforcing bounds (-1, 1).

## Architectural Constraints

- Flat Data Structure: Each pillar computed independently
- Deterministic Lower Bounds: All math via scaled tanh
- Absolute Temporal Alignment: Max date 2025-06-30; zero 2026 lookahead
- Zero Hardcoded Values: Weights from Spearman ρ optimization on historical slice
