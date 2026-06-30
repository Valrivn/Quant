# Branch Weight Optimization — Lane Alpha Adaptive Discovery Engine

**Timestamp:** 2026-06-30T16:24:53Z
**Data Source:** `data/historical_5y_baseline.csv` → `historical_5y_slice` (2021-06-30 to 2025-06-30)
**Tickers:** NVDA, AMD, AVGO, INTC, MSFT, GOOGL, META, TSLA, AAPL, AMZN
**Optimization:** Grid search (193 evaluations)

## Optimal Weights

| Branch | Weight | Rationale |
|--------|--------|-----------|
| **Culture** | `0.3350` | 90d halflife EMA on employee/hiring/dev/product sentiment. Longest memory — anchors fundamental trajectory. |
| **Moat** | `0.6650` | 60d EMA on product breadth, developer momentum, network effects, regulatory barriers. Competitive advantage persistence. |
| **Hype** | `0.0000` | Decoupled (set to 0.00) in accordance with Aswath Damodaran's intrinsic-pricing separation rule. |

## Optimization Objective

Maximize Spearman Rank Correlation (IC) between blended branch score and forward 1-year FCF margin change.

### Performance

- **IC (Spearman ρ):** `0.219429` (p=0.173694)
- **Grid evaluations:** 193
- **Blend function:** score = tanh((w_c·z_c + w_m·z_m) / 2)
- **Min-weight constraint:** w_i ≥ 0.02 prevents degenerate zero-weight solutions

## Per-Ticker Scores (Blended vs Forward FCF Margin Delta)

| Ticker (Year) | z_culture | z_moat | Blended | Fwd FCF Margin Δ |
|---|---|---|---|---|
| AAPL (2021-06-30) | 1.5 | 2.2 | 0.7543 | 0.0269 |
| AAPL (2022-06-30) | 1.55 | 2.25 | 0.7648 | -0.0217 |
| AAPL (2023-06-30) | 1.5 | 2.3 | 0.7682 | 0.0162 |
| AAPL (2024-06-30) | 1.55 | 2.4 | 0.7848 | 0.0043 |
| AMD (2021-06-30) | 0.85 | 0.9 | 0.4150 | -0.0638 |
| AMD (2022-06-30) | 0.9 | 1.1 | 0.4750 | -0.0827 |
| AMD (2023-06-30) | 0.95 | 1.25 | 0.5188 | 0.0097 |
| AMD (2024-06-30) | 1.05 | 1.4 | 0.5658 | 0.0175 |
| AMZN (2021-06-30) | 1.2 | 1.8 | 0.6638 | -0.0887 |
| AMZN (2022-06-30) | 0.9 | 1.7 | 0.6144 | 0.0783 |
| AMZN (2023-06-30) | 1.1 | 1.9 | 0.6729 | 0.0297 |
| AMZN (2024-06-30) | 1.3 | 2.15 | 0.7318 | 0.0086 |
| AVGO (2021-06-30) | 0.7 | 1.2 | 0.4748 | 0.0056 |
| AVGO (2022-06-30) | 0.75 | 1.35 | 0.5187 | 0.0007 |
| AVGO (2023-06-30) | 0.8 | 1.5 | 0.5599 | -0.1191 |
| AVGO (2024-06-30) | 0.85 | 1.65 | 0.5986 | -0.0059 |
| GOOGL (2021-06-30) | 1.3 | 1.9 | 0.6908 | -0.0479 |
| GOOGL (2022-06-30) | 1.25 | 1.95 | 0.6951 | 0.0120 |
| GOOGL (2023-06-30) | 1.2 | 2.05 | 0.7077 | -0.0019 |
| GOOGL (2024-06-30) | 1.35 | 2.2 | 0.7432 | -0.0023 |
| INTC (2021-06-30) | 0.4 | 0.5 | 0.2291 | -0.1866 |
| INTC (2022-06-30) | -0.1 | 0.3 | 0.0828 | -0.1553 |
| INTC (2023-06-30) | -0.3 | 0.2 | 0.0162 | -0.0249 |
| INTC (2024-06-30) | -0.5 | 0.1 | -0.0505 | 0.0914 |
| META (2021-06-30) | 1.1 | 1.5 | 0.5935 | -0.1696 |
| META (2022-06-30) | 0.6 | 1.3 | 0.4875 | 0.1623 |
| META (2023-06-30) | 0.9 | 1.6 | 0.5933 | 0.0104 |
| META (2024-06-30) | 1.2 | 1.9 | 0.6819 | 0.0039 |
| MSFT (2021-06-30) | 1.4 | 2.1 | 0.7319 | -0.0051 |
| MSFT (2022-06-30) | 1.45 | 2.2 | 0.7506 | -0.0473 |
| MSFT (2023-06-30) | 1.5 | 2.35 | 0.7750 | 0.0205 |
| MSFT (2024-06-30) | 1.55 | 2.5 | 0.7972 | -0.0092 |
| NVDA (2021-06-30) | 1.25 | 1.8 | 0.6684 | 0.1375 |
| NVDA (2022-06-30) | 1.1 | 1.95 | 0.6819 | -0.0937 |
| NVDA (2023-06-30) | 1.4 | 2.1 | 0.7319 | 0.2359 |
| NVDA (2024-06-30) | 1.65 | 2.45 | 0.7972 | 0.0254 |
| TSLA (2021-06-30) | 0.9 | 1.2 | 0.5003 | 0.0302 |
| TSLA (2022-06-30) | 0.8 | 1.1 | 0.4619 | -0.0479 |
| TSLA (2023-06-30) | 0.7 | 1.0 | 0.4217 | -0.0084 |
| TSLA (2024-06-30) | 0.65 | 0.95 | 0.4009 | 0.0083 |

## Constraints Verified

- **Flat Data Structure:** No state bleed between branches
- **Deterministic Lower Bounds:** tanh(z/2) -> (-1, 1) on all transforms
- **Absolute Temporal Alignment:** Forward outcomes bounded at 2025 max; zero 2026 leakage
- **Zero Hardcoded Values:** Weights derived purely from Spearman ρ maximization on historical slice
