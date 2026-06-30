# Branch Weight Optimization — Lane Alpha Adaptive Discovery Engine

**Timestamp:** 2026-06-30T10:43:13Z
**Data Source:** `data/historical_5y_baseline.csv` → `historical_5y_slice` (2021-06-30 to 2025-06-30)
**Tickers:** NVDA, AMD, AVGO, INTC, MSFT, GOOGL, META, TSLA, AAPL, AMZN
**Optimization:** Grid search (17743 evaluations)

## Optimal Weights

| Branch | Weight | Rationale |
|--------|--------|-----------|
| **Culture** | `0.0200` | 90d halflife EMA on employee/hiring/dev/product sentiment. Longest memory — anchors fundamental trajectory. |
| **Moat** | `0.5050` | 60d EMA on product breadth, developer momentum, network effects, regulatory barriers. Competitive advantage persistence. |
| **Hype** | `0.4750` | 21d halflife EMA on reddit velocity, bull/bear ratio, mention velocity, social sentiment. Shortest decay, highest frequency. |

## Optimization Objective

Maximize Spearman Rank Correlation (IC) between blended branch score and forward 1-year FCF margin change.

### Performance

- **IC (Spearman ρ):** `0.270356` (p=0.091556)
- **Grid evaluations:** 17743
- **Blend function:** score = tanh((w_c·z_c + w_m·z_m + w_h·z_h) / 2)
- **Min-weight constraint:** w_i ≥ 0.02 prevents degenerate zero-weight solutions

## Per-Ticker Scores (Blended vs Forward FCF Margin Delta)

| Ticker (Year) | z_culture | z_moat | z_hype | Blended | Fwd FCF Margin Δ |
|---|---|---|---|---|---|
| AAPL (2021-06-30) | 1.5 | 2.2 | 0.6 | 0.6126 | 0.0269 |
| AAPL (2022-06-30) | 1.55 | 2.25 | 0.65 | 0.6279 | -0.0217 |
| AAPL (2023-06-30) | 1.5 | 2.3 | 0.6 | 0.6281 | 0.0162 |
| AAPL (2024-06-30) | 1.55 | 2.4 | 0.7 | 0.6571 | 0.0043 |
| AMD (2021-06-30) | 0.85 | 0.9 | 0.3 | 0.2977 | -0.0638 |
| AMD (2022-06-30) | 0.9 | 1.1 | 0.4 | 0.3642 | -0.0827 |
| AMD (2023-06-30) | 0.95 | 1.25 | 0.5 | 0.4169 | 0.0097 |
| AMD (2024-06-30) | 1.05 | 1.4 | 0.7 | 0.4856 | 0.0175 |
| AMZN (2021-06-30) | 1.2 | 1.8 | 0.5 | 0.5265 | -0.0887 |
| AMZN (2022-06-30) | 0.9 | 1.7 | 0.2 | 0.4508 | 0.0783 |
| AMZN (2023-06-30) | 1.1 | 1.9 | 0.45 | 0.5354 | 0.0297 |
| AMZN (2024-06-30) | 1.3 | 2.15 | 0.7 | 0.6182 | 0.0086 |
| AVGO (2021-06-30) | 0.7 | 1.2 | 0.1 | 0.3219 | 0.0056 |
| AVGO (2022-06-30) | 0.75 | 1.35 | 0.15 | 0.3662 | 0.0007 |
| AVGO (2023-06-30) | 0.8 | 1.5 | 0.2 | 0.4089 | -0.1191 |
| AVGO (2024-06-30) | 0.85 | 1.65 | 0.3 | 0.4593 | -0.0059 |
| GOOGL (2021-06-30) | 1.3 | 1.9 | 0.4 | 0.5283 | -0.0479 |
| GOOGL (2022-06-30) | 1.25 | 1.95 | 0.35 | 0.5285 | 0.0120 |
| GOOGL (2023-06-30) | 1.2 | 2.05 | 0.45 | 0.5625 | -0.0019 |
| GOOGL (2024-06-30) | 1.35 | 2.2 | 0.6 | 0.6116 | -0.0023 |
| INTC (2021-06-30) | 0.4 | 0.5 | -0.2 | 0.0826 | -0.1866 |
| INTC (2022-06-30) | -0.1 | 0.3 | -0.4 | -0.0202 | -0.1553 |
| INTC (2023-06-30) | -0.3 | 0.2 | -0.5 | -0.0711 | -0.0249 |
| INTC (2024-06-30) | -0.5 | 0.1 | -0.6 | -0.1216 | 0.0914 |
| META (2021-06-30) | 1.1 | 1.5 | 0.3 | 0.4309 | -0.1696 |
| META (2022-06-30) | 0.6 | 1.3 | -0.2 | 0.2791 | 0.1623 |
| META (2023-06-30) | 0.9 | 1.6 | 0.4 | 0.4684 | 0.0104 |
| META (2024-06-30) | 1.2 | 1.9 | 0.75 | 0.5849 | 0.0039 |
| MSFT (2021-06-30) | 1.4 | 2.1 | 0.5 | 0.5804 | -0.0051 |
| MSFT (2022-06-30) | 1.45 | 2.2 | 0.55 | 0.6048 | -0.0473 |
| MSFT (2023-06-30) | 1.5 | 2.35 | 0.7 | 0.6496 | 0.0205 |
| MSFT (2024-06-30) | 1.55 | 2.5 | 0.8 | 0.6841 | -0.0092 |
| NVDA (2021-06-30) | 1.25 | 1.8 | 0.45 | 0.5182 | 0.1375 |
| NVDA (2022-06-30) | 1.1 | 1.95 | 0.6 | 0.5689 | -0.0937 |
| NVDA (2023-06-30) | 1.4 | 2.1 | 1.2 | 0.6801 | 0.2359 |
| NVDA (2024-06-30) | 1.65 | 2.45 | 1.8 | 0.7867 | 0.0254 |
| TSLA (2021-06-30) | 0.9 | 1.2 | 1.8 | 0.6288 | 0.0302 |
| TSLA (2022-06-30) | 0.8 | 1.1 | 1.5 | 0.5663 | -0.0479 |
| TSLA (2023-06-30) | 0.7 | 1.0 | 0.9 | 0.4408 | -0.0084 |
| TSLA (2024-06-30) | 0.65 | 0.95 | 0.6 | 0.3704 | 0.0083 |

## Constraints Verified

- **Flat Data Structure:** No state bleed between branches
- **Deterministic Lower Bounds:** tanh(z/2) -> (-1, 1) on all transforms
- **Absolute Temporal Alignment:** Forward outcomes bounded at 2025 max; zero 2026 leakage
- **Zero Hardcoded Values:** Weights derived purely from Spearman ρ maximization on historical slice
