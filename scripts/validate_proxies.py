"""Proxy Validation & Health Check Tool.

Validates configured proxy endpoints, measures latency, checks success rates,
tests IP rotation, and recommends an optimal proxy pool for Google Trends scraping.

Usage:
    python scripts/validate_proxies.py --config config/google_trends_config.json
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.google_trends import build_proxy_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def test_ip_lookup(session: requests.Session, timeout: float = 10.0) -> Optional[str]:
    """Retrieve public IP address through the active proxy session."""
    ip_services = [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
        "https://ifconfig.me/all.json",
    ]
    for service in ip_services:
        try:
            res = session.get(service, timeout=timeout)
            if res.status_code == 200:
                data = res.json()
                if "ip" in data:
                    return data["ip"]
                elif "origin" in data:
                    return data["origin"].split(",")[0].strip()
        except Exception:
            continue
    return None


def test_google_trends_reachability(session: requests.Session, timeout: float = 10.0) -> Tuple[bool, int, str]:
    """Test reachability of Google Trends main page and check for CAPTCHA/block."""
    url = "https://trends.google.com/trends/"
    try:
        res = session.get(url, timeout=timeout)
        status_code = res.status_code
        text_lower = res.text.lower()
        if "captcha" in text_lower or "unusual traffic" in text_lower or "google.com/sorry" in text_lower:
            return False, status_code, "CAPTCHA / Block detected"
        if status_code in (200, 302):
            return True, status_code, "OK"
        return False, status_code, f"HTTP {status_code}"
    except Exception as exc:
        return False, 0, str(exc)


def validate_single_proxy(
    proxy_config: Dict[str, Any],
    timeout: float = 10.0,
    test_google: bool = True,
    trials: int = 2,
) -> Dict[str, Any]:
    """Validate a single proxy config entry across multiple trials to measure latency and IP rotation."""
    provider = proxy_config.get("provider", "generic")
    host = proxy_config.get("host", "")
    port = proxy_config.get("port", "")
    user = proxy_config.get("user", "")
    has_credentials = bool(user and user != "..." and not user.endswith("_USER") or os.environ.get(f"{provider.upper()}_USER"))

    proxy_url = build_proxy_url(proxy_config)

    result = {
        "provider": provider,
        "host": host,
        "port": port,
        "has_credentials": has_credentials,
        "proxy_url_formatted": bool(proxy_url),
        "ip_addresses": [],
        "latencies_ms": [],
        "success_count": 0,
        "trials": trials,
        "google_reachable": False,
        "status": "UNKNOWN",
        "notes": "",
    }

    if provider == "serpapi":
        api_key = proxy_config.get("api_key", "")
        if api_key.startswith("ENV:"):
            api_key = os.environ.get(api_key[4:], "")
        elif not api_key:
            api_key = os.environ.get("SERPAPI_KEY", "")

        if not api_key:
            result["status"] = "NO_API_KEY"
            result["notes"] = "SerpAPI API key not set (set SERPAPI_KEY env var)"
            return result

        # Test SerpAPI endpoint
        start_t = time.time()
        try:
            res = requests.get(f"https://serpapi.com/search.json?engine=google_trends&q=test&api_key={api_key}", timeout=timeout)
            elapsed = (time.time() - start_t) * 1000.0
            if res.status_code == 200:
                result["status"] = "HEALTHY"
                result["google_reachable"] = True
                result["success_count"] = 1
                result["latencies_ms"].append(round(elapsed, 2))
                result["notes"] = "SerpAPI Google Trends engine online"
            else:
                result["status"] = f"HTTP_{res.status_code}"
                result["notes"] = f"SerpAPI returned HTTP {res.status_code}"
        except Exception as exc:
            result["status"] = "ERROR"
            result["notes"] = f"SerpAPI connection error: {exc}"
        return result

    if not has_credentials and (user.startswith("ENV:") or user == "..."):
        result["status"] = "UNCONFIGURED"
        result["notes"] = f"Environment variable credentials not set for {provider}"
        return result

    if not proxy_url:
        result["status"] = "UNCONFIGURED"
        result["notes"] = "Missing credentials or host/port configuration"
        return result

    session = requests.Session()
    session.proxies = {"http": proxy_url, "https": proxy_url}
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })

    ips = set()
    for trial in range(trials):
        start_t = time.time()
        ip = test_ip_lookup(session, timeout=timeout)
        elapsed = (time.time() - start_t) * 1000.0
        if ip:
            ips.add(ip)
            result["latencies_ms"].append(round(elapsed, 2))
            result["success_count"] += 1
        time.sleep(0.5)

    result["ip_addresses"] = list(ips)

    if test_google:
        g_ok, g_status, g_msg = test_google_trends_reachability(session, timeout=timeout)
        result["google_reachable"] = g_ok
        if not g_ok:
            result["notes"] = f"Google Trends check failed: {g_msg}"

    if result["success_count"] > 0 and (not test_google or result["google_reachable"]):
        result["status"] = "HEALTHY"
    elif result["success_count"] > 0:
        result["status"] = "BLOCKED_BY_GOOGLE"
    else:
        result["status"] = "UNREACHABLE"
        if not result["notes"]:
            result["notes"] = "Connection refused or timed out"

    return result


def main():
    parser = argparse.ArgumentParser(description="Validate Proxy Pool Endpoints for Google Trends Scraper")
    parser.add_argument(
        "--config",
        type=str,
        default=str(PROJECT_ROOT / "config" / "google_trends_config.json"),
        help="Path to google_trends_config.json",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds for proxy requests",
    )
    parser.add_argument(
        "--no-google-check",
        action="store_true",
        help="Skip testing Google Trends reachability",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="",
        help="Path to save validation results JSON report",
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    proxy_list = config_data.get("proxies", [])

    print("=" * 80)
    print("RESIDENTIAL PROXY POOL VALIDATION & HEALTH CHECK")
    print("=" * 80)
    print(f"Loaded {len(proxy_list)} proxy configuration(s) from {config_path.name}\n")

    results = []
    recommended_pool = []

    for idx, p_conf in enumerate(proxy_list, 1):
        provider = p_conf.get("provider", "generic")
        print(f"[{idx}/{len(proxy_list)}] Testing {provider.upper()} proxy endpoint...")
        res = validate_single_proxy(
            p_conf,
            timeout=args.timeout,
            test_google=not args.no_google_check,
        )
        results.append(res)

        avg_lat = round(sum(res["latencies_ms"]) / len(res["latencies_ms"]), 2) if res["latencies_ms"] else 0.0
        status_symbol = "[OK]" if res["status"] == "HEALTHY" else "[X]"
        print(f"    Status:           {status_symbol} {res['status']}")
        print(f"    Google Reachable: {'YES' if res['google_reachable'] else 'NO'}")
        print(f"    Avg Latency:      {avg_lat} ms")
        print(f"    IPs Observed:     {res['ip_addresses'] if res['ip_addresses'] else 'None'}")
        if res["notes"]:
            print(f"    Notes:            {res['notes']}")
        print()

        if res["status"] == "HEALTHY":
            recommended_pool.append(p_conf)

    print("=" * 80)
    print("VALIDATION SUMMARY & RECOMMENDATION")
    print("=" * 80)
    print(f"Total Proxies Tested:  {len(results)}")
    healthy_count = sum(1 for r in results if r["status"] == "HEALTHY")
    print(f"Healthy & Ready:       {healthy_count} / {len(results)}")

    if healthy_count == 0:
        print("\n[!] No active proxy credentials detected or proxies blocked.")
        print("    To activate residential proxies, supply your provider credentials via environment variables:")
        print("    - export OXYLABS_USER='...' OXYLABS_PASS='...'")
        print("    - export BRIGHTDATA_USER='...' BRIGHTDATA_PASS='...'")
        print("    - export SERPAPI_KEY='...'")
    else:
        print(f"\n[OK] Recommended Proxy Pool Size: {len(recommended_pool)}")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_tested": len(results),
            "healthy_count": healthy_count,
            "results": results,
            "recommended_pool": recommended_pool,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)
        print(f"\nSaved validation report to {out_path}")


if __name__ == "__main__":
    main()
