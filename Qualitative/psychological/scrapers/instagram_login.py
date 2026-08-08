"""One-time Instagram login helper (D-20260807-002).

Opens a VISIBLE browser to instagram.com so the CEO can log in by hand, then
persists the session cookies to ``config/instagram_cookies.json`` in exactly the
schema ``InstagramSession.load_cookies`` expects (name/value/domain/path/secure/
expires). The scraper's fail-closed gate requires this file before any live
fetch.

Usage (from repo root):
    python -m psychological.scrapers.instagram_login

The helper polls for the ``sessionid`` cookie and exits once login is detected.
It never stores credentials; it only saves the browser's cookie jar.
"""

import asyncio
import json
import logging
import os
import time

from config import load_hybrid_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SESSION_FILE = "config/instagram_cookies.json"


def _session_file() -> str:
    try:
        cfg = load_hybrid_config().get("psychological", {}).get("instagram", {})
        return str(cfg.get("session_file", DEFAULT_SESSION_FILE))
    except Exception:  # noqa: BLE001 - config optional
        return DEFAULT_SESSION_FILE


async def _cookies_to_file(tab, path: str) -> None:
    jar = getattr(tab, "cookies", None)
    if jar is None:
        raise RuntimeError("nodriver cookie API unavailable on this tab")
    cookies = await jar.get_all()
    out = []
    for c in cookies:
        out.append({
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", ""),
            "domain": getattr(c, "domain", None),
            "path": getattr(c, "path", None),
            "secure": bool(getattr(c, "secure", False)),
            "expires": getattr(c, "expires", None),
        })
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    logger.info("Saved %d cookies to %s", len(out), path)


async def _has_sessionid(tab) -> bool:
    try:
        jar = getattr(tab, "cookies", None)
        if jar is None:
            return False
        cookies = await jar.get_all()
        names = {getattr(c, "name", "") for c in cookies}
        return "sessionid" in names
    except Exception:  # noqa: BLE001 - poll must never crash
        return False


async def login() -> str:
    import nodriver as uc

    browser_executable = os.getenv("CHROME_BINARY_PATH")
    if not browser_executable:
        try:
            hybrid = load_hybrid_config()
            browser_executable = hybrid.get("psychological", {}).get("browser_binary_path")
        except Exception:  # noqa: BLE001
            pass
    if not browser_executable:
        for candidate in (
            os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        ):
            if candidate and os.path.exists(candidate):
                browser_executable = candidate
                break

    browser_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-component-update",
    ]
    logger.info("Launching visible browser (headless=False) ...")
    browser = await uc.start(
        headless=False,
        browser_executable_path=browser_executable,
        browser_args=browser_args,
    )
    tab = await browser.get("https://www.instagram.com/")
    logger.info("Log in to Instagram in the browser window that just opened.")
    logger.info("Waiting for the sessionid cookie (polls every 5s, up to 10 min) ...")

    deadline = time.time() + 600
    while time.time() < deadline:
        if await _has_sessionid(tab):
            path = _session_file()
            await _cookies_to_file(tab, path)
            logger.info("Login captured. Cookies written to %s", path)
            await browser.stop()
            return path
        await asyncio.sleep(5)

    await browser.stop()
    raise TimeoutError(
        "No sessionid cookie detected within 10 minutes. Re-run and log in, "
        "or check that two-factor verification is completed."
    )


if __name__ == "__main__":
    result = asyncio.run(login())
    print("Done:", result)
