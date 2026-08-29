"""Manual account-registration helper (D-20260816-001).

Opens each platform's signup page in your REAL default browser, one at a time,
so a human completes the CAPTCHA / email-verification / ToS steps. Progress is
persisted to a git-ignored file so you can resume anytime.

Controls:
    ENTER        mark this platform done, move to the next
    s            skip (leave it pending)
    q            quit and save progress
    r            replay / reopen the current URL
    h            show this help

The script NEVER submits forms or automates the browser — it only opens URLs.
Use your alt Gmail (minidragonminidragon@gmail.com) for the session-account
platforms (Glassdoor, LinkedIn).
"""

import json
import os
import sys
import webbrowser

# Where checkoff progress is stored (git-ignored).
_PROGRESS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "registration_progress.json"
)

# cost: "free" | "paid" | "public" (public = no account needed at all)
# env:  the .env key it populates, or None
PLATFORMS = [
    # --- Session accounts (use the alt Gmail) --------------------------------
    {"name": "Glassdoor", "url": "https://www.glassdoor.com/profile/login_input.htm",
     "env": "GLASSDOOR_EMAIL/PASSWORD", "cost": "free",
     "note": "Burner account. NodeDriver strategy + residential proxy needed to scrape."},
    {"name": "LinkedIn", "url": "https://www.linkedin.com/signup",
     "env": "LINKEDIN_EMAIL/PASSWORD", "cost": "free",
     "note": "Burner account. Powers the talent scout (senior-join mentions)."},
    {"name": "TikTok", "url": "https://www.tiktok.com/signup",
     "env": "TIKTOK_SESSION_ID", "cost": "free",
     "note": "Session cookie for employer-mention talent surface."},
    {"name": "Indeed", "url": "https://www.indeed.com/",
     "env": None, "cost": "free",
     "note": "No account needed for the Google JSON-LD + NodeDriver path."},

    # --- API keys (free dev accounts) ----------------------------------------
    {"name": "Adzuna Developer", "url": "https://developer.adzuna.com/",
     "env": "ADZUNA_APP_ID / ADZUNA_APP_KEY", "cost": "free",
     "note": "Job-count API for hiring velocity. Grab app_id + app_key."},
    {"name": "Trustpilot Developers", "url": "https://developers.trustpilot.com/",
     "env": "TRUSTPILOT_API_KEY", "cost": "free",
     "note": "Business-units API key. Preferred over scraping (aggressive bot blocks)."},
    {"name": "Google Patents API", "url": "https://developers.google.com/patents",
     "env": "GOOGLE_PATENTS_API_KEY", "cost": "free",
     "note": "Deep-tech R&D. Use the alt Gmail for the Google Cloud project."},
    {"name": "Semantic Scholar", "url": "https://www.semanticscholar.org/product/api",
     "env": "SEMANTIC_SCHOLAR_API_KEY", "cost": "free",
     "note": "Paper citations / R&D signal (optional key, public endpoint exists)."},
    {"name": "OpenAlex", "url": "https://openalex.org/",
     "env": None, "cost": "public",
     "note": "Free scholarly metadata API. No account."},
    {"name": "USPTO PatentsView", "url": "https://developer.uspto.gov/",
     "env": "USPTO_API_KEY", "cost": "free",
     "note": "Patent filings / innovation signal."},

    # --- Paid panels / APIs (contact sales or buy subscription) --------------
    {"name": "Bloomberg Second Measure", "url": "https://secondmeasure.com/",
     "env": "BSM_API_KEY", "cost": "paid",
     "note": "Consumer transaction panel. Strongest Type-A revenue signal."},
    {"name": "Facteus", "url": "https://www.facteus.com/",
     "env": "FACTEUS_API_KEY", "cost": "paid",
     "note": "Anonymized card-transaction panel."},
    {"name": "Consumer Edge", "url": "https://www.consumer-edge.com/",
     "env": "CONSUMER_EDGE_API_KEY", "cost": "paid",
     "note": "Transactional consumer data."},
    {"name": "YipitData", "url": "https://www.yipitdata.com/",
     "env": "YIPIT_API_KEY", "cost": "paid",
     "note": "Alternative data aggregator."},
    {"name": "Similarweb", "url": "https://developer.similarweb.com/",
     "env": "SIMILARWEB_API_KEY", "cost": "paid",
     "note": "Web traffic. Auto-dropped under ~50k monthly visits (floor filter)."},
    {"name": "Sensor Tower", "url": "https://sensortower.com/",
     "env": "SENSOR_TOWER_API_KEY", "cost": "paid",
     "note": "App store download/revenue estimates."},
    {"name": "data.ai", "url": "https://www.data.ai/",
     "env": "DATAAI_API_KEY", "cost": "paid",
     "note": "App intelligence (formerly App Annie)."},

    # --- B2B / industrial / logistics ----------------------------------------
    {"name": "ThomasNet", "url": "https://www.thomasnet.com/",
     "env": None, "cost": "public",
     "note": "Industrial supplier listings. Public browse."},
    {"name": "Global Sources", "url": "https://www.globalsources.com/",
     "env": None, "cost": "free",
     "note": "Sourcing trade listings."},
    {"name": "IndustryNet", "url": "https://www.industrynet.com/",
     "env": None, "cost": "public",
     "note": "Industrial directory. Public browse."},
    {"name": "ImportGenius", "url": "https://www.importgenius.com/",
     "env": "IMPORTGENIUS_API_KEY", "cost": "paid",
     "note": "Customs / bills of lading. Type-A official manifests."},
    {"name": "Panjiva", "url": "https://panjiva.com/",
     "env": "PANJIVA_API_KEY", "cost": "paid",
     "note": "Supply-chain trade data (S&P Global)."},

    # --- Healthcare / research (public or free API) --------------------------
    {"name": "ClinicalTrials.gov API", "url": "https://clinicaltrials.gov/data-api/api",
     "env": None, "cost": "public",
     "note": "Government trial registry. Public API v2, no key."},
    {"name": "FDA FAERS", "url": "https://www.fda.gov/drugs/",
     "env": None, "cost": "public",
     "note": "Adverse-event reports. Public data dump."},
    {"name": "PubMed E-utilities", "url": "https://www.ncbi.nlm.nih.gov/home/develop/api/",
     "env": None, "cost": "public",
     "note": "Biomedical literature. Public API, optional key for higher rate."},

    # --- Brand / reputation ---------------------------------------------------
    {"name": "YouGov BrandIndex", "url": "https://www.yougov.com/",
     "env": "YOUGOV_API_KEY", "cost": "paid",
     "note": "Survey-based brand perception (small-sample caveat)."},
    {"name": "BBB", "url": "https://www.bbb.org/",
     "env": None, "cost": "public",
     "note": "Self-reported resolution data — tripwire only, never a pass-gate."},
    {"name": "G2", "url": "https://www.g2.com/",
     "env": "G2_API_KEY", "cost": "paid",
     "note": "Reliable-ish review platform; Gartner API preferred over scraping."},
    {"name": "Capterra", "url": "https://www.capterra.com/",
     "env": "CAPTERRA_API_KEY", "cost": "paid",
     "note": "SMB review platform (Gartner-owned)."},

    # --- Consumer / hardware (mostly public scrape) --------------------------
    {"name": "Amazon (Seller Central)", "url": "https://sellercentral.amazon.com/",
     "env": None, "cost": "free",
     "note": "SKU velocity signal. Public page scrape + optional seller account."},
    {"name": "Google Merchant Center", "url": "https://merchants.google.com/",
     "env": None, "cost": "free",
     "note": "Use the alt Gmail. Product availability / price data."},
    {"name": "Best Buy", "url": "https://www.bestbuy.com/",
     "env": None, "cost": "public",
     "note": "Hardware reviews. Public page scrape."},
    {"name": "B&H Photo", "url": "https://www.bhphotovideo.com/",
     "env": None, "cost": "public",
     "note": "Hardware reviews. Public page scrape."},
]


def _load_progress() -> dict:
    if os.path.exists(_PROGRESS_FILE):
        try:
            with open(_PROGRESS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_progress(progress: dict) -> None:
    os.makedirs(os.path.dirname(_PROGRESS_FILE), exist_ok=True)
    with open(_PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def _open(url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
        if not opened:
            os.startfile(url)  # Windows fallback
    except Exception as exc:  # noqa: BLE001
        print(f"  !! could not open browser: {exc}")
        print(f"  !! open manually: {url}")


def main() -> None:
    progress = _load_progress()
    done = progress.get("done", [])

    pending = [p for p in PLATFORMS if p["name"] not in done]
    if not pending:
        print("All platforms already checked off. Delete data/registration_progress.json to restart.")
        return

    print(f"=== Registration helper: {len(pending)} platforms pending ===")
    print("Use your alt Gmail for session accounts. ENTER = done, s = skip, q = quit, r = reopen, h = help\n")

    for p in pending:
        name, url, env, cost, note = (
            p["name"], p["url"], p.get("env"), p.get("cost"), p.get("note"),
        )
        while True:
            print(f"\n[{name}]  ({cost})")
            print(f"  env: {env or '— none —'}")
            print(f"  note: {note}")
            print(f"  URL: {url}")
            _open(url)
            key = input("  [ENTER=done, s=skip, r=reopen, q=quit] > ").strip().lower()
            if key in ("q", "quit"):
                print("Progress saved.")
                return
            if key in ("r", "reopen"):
                continue
            if key in ("", "y", "yes"):
                done.append(name)
                progress["done"] = done
                _save_progress(progress)
                print(f"  ✓ {name} done")
            break

    print("\n=== All pending platforms visited. Re-run to continue/resume. ===")
    print("Then paste any API keys into .env (docs/consensus-gate-env-requirements.md).")


if __name__ == "__main__":
    main()