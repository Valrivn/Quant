# Anti-Bias Alt-Data Consensus Gate — Environment / Account Requirements

The consensus gate collects from three evidence tiers. Below is exactly what you
need to store in `.env` (repo root, git-ignored) to unlock each source. The
pipeline is **fail-closed**: a missing key degrades that source (zeroed
contribution + ledger entry), never a hard stop.

## Tier A — Official / transactional (pass-capable, highest trust)

| Source | Required env keys | What to store |
|---|---|---|
| **SEC EDGAR** (10-K attrition, CIK resolution) | none (public) | No account. Uses `valuation_alpha/universe/cik_resolver.py`. Rate: 0.5 req/s. |
| **Transaction panels** (Bloomberg Second Measure / Facteus / Consumer Edge / YipitData) | `BSM_API_KEY`, `FACTEUS_API_KEY`, `CONSUMER_EDGE_API_KEY`, `YIPIT_API_KEY` | API keys (paid subscription per panel). If you have only one, only that factor fires. |
| **ImportGenius / Panjiva** (customs/bills of lading) | `IMPORTGENIUS_API_KEY`, `PANJIVA_API_KEY` | Customs-trade API keys (both paid). |

## Tier B — Modeled / directional (never pass-capable alone)

| Source | Required env keys | What to store |
|---|---|---|
| **Adzuna** (job count → hiring velocity) | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Free dev keys from developer.adzuna.com (US region: `adzuna-*` app). |
| **JobSpy** (LinkedIn/Indeed job wrappers) | none | Keyless (scrapes public job search). Works if JobSpy installed. |
| **LinkedIn talent scout** (senior-join mentions) | `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` | **Session account.** NodeDriver logs in; CDP stealth applied. LinkedIn is the most bot-guarded — expect occasional CAPTCHA. Keep a *burner* account, never your real one. |
| **Similarweb / Sensor Tower** (web/app telemetry) | `SIMILARWEB_API_KEY`, `SENSOR_TOWER_API_KEY` | Paid API keys. **Flag**: unreliable below ~50k monthly visits (small-cap range) — floor filter drops these automatically. |
| **Instagram / Reddit / TikTok** (employer-mention talent surfaces) | IG: `config/instagram_cookies.json` (existing). Reddit: `config/reddit_credentials.yaml`. TikTok: `TIKTOK_SESSION_ID` | IG/Reddit accounts already wired in the pipeline; TikTok is a session cookie from an anonymous browser. |

## Tier C — Review platforms (2-of-3 convergence required)

| Source | Required env keys | What to store |
|---|---|---|
| **Indeed** | none | No account (Google JSON-LD + NodeDriver fallback path). |
| **Glassdoor** | `GLASSDOOR_EMAIL`, `GLASSDOOR_PASSWORD` | **Session account.** Glassdoor hard-blocks datacenter IPs and runs Cloudflare/Turnstile. The NodeDriver strategy warms up the homepage on the same tab to clear the challenge, then navigates to the target page with CDP evasion. Use a burner account + residential proxy. |
| **G2 / Capterra** | `G2_API_KEY`, `CAPTERRA_API_KEY` | Paid G2/Gartner APIs (recommended over scraping). Without keys, the light/JSON-LD path is used. |
| **Trustpilot** | `TRUSTPILOT_API_KEY` | Trustpilot offers a public API (business units). Scraping is aggressively blocked — prefer the API key. |
| **Comparably / Levels.fyi / Blind** | none | Public pages; light/NodeDriver path. No account needed (low block risk). |

## Anti-bot infra (shared)

| Purpose | env keys | Notes |
|---|---|---|
| **Browser binary** (NodeDriver) | `CHROME_BINARY_PATH` | Path to Chrome/Brave/Edge executable. If unset, uses Brave default path. **This is the NodeDriver strategy's key lever** — a real installed browser with a real profile defeats most Cloudflare checks. |
| **Proxies** | `PROXY_LIST`, `PROXY_USERNAME`, `PROXY_PASSWORD` | Optional. `PROXY_LIST` = comma-separated `ip:port` or `user:pass@ip:port`. If unset, ProxyManager falls back to free lists (weak). **For Glassdoor/LinkedIn use residential proxies.** |
| **Live gate** | `DISCOVERY_LIVE=1` | **Required** to run any live collection. Without it every collector degrades (by design). |
| **GEMINI_API_KEY** | `GEMINI_API_KEY` | Only needed if the LLM proxy-synthesis stage is also live (IG_LLM pipeline). |

## Accounts you must actually create (summary)

1. **Glassdoor** — 1 burner account (Google-signup is fastest; keep a separate email).
2. **LinkedIn** — 1 burner account (fresh profile, NOT your personal one).
3. **Adzuna** — 1 free dev account (app_id + app_key).
4. **Trustpilot** — 1 free API key.
5. Optional paid: 1 transaction panel (BSM/Facteus) for the strongest Tier-A signal; G2 API; ImportGenius/Panjiva for logistics.

## Minimal `.env` to make the gate actually collect (recommended start)

```
DISCOVERY_LIVE=1
CHROME_BINARY_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
ADZUNA_APP_ID=...
ADZUNA_APP_KEY=...
TRUSTPILOT_API_KEY=...
GLASSDOOR_EMAIL=...
GLASSDOOR_PASSWORD=...
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...
```

Everything else degrades gracefully to zeroed (logged) contributions until you
add the key. The consensus still runs on whatever evidence tier is live —
that's the whole anti-bias point: **a missing source abstains, it never votes.**