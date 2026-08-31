# Lane Gamma Data Completeness Audit

Generated: 2026-08-31 02:15:52 UTC
SEC lookback: 5d | GitHub lookback: 50d | Elapsed: 123.4s

| Ticker | SEC | GitHub | Glassdoor | Reddit | Coverage | Rating |
|--------|-----|--------|-----------|--------|----------|--------|
| NVDA | OK | OK | OK | OK | 4/4 sources | 4.2 |
| AVGO | MISSING | MISSING | BLOCKED_403 | MISSING | 0/4 sources | N/A |

## Guard & Alignment Notes

- `tanh_clamp` keeps every z-score inside [-1, 1].
- `PublicationLagMatrix` shifts late-reporting sources before alignment.
- Glassdoor fetch uses the CF Bypass Strategy 3 (distributed SERP API).
