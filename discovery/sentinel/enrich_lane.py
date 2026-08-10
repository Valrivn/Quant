"""Enrichment lane — G4: IP-safe web pass-through via Jina Reader.

Glassdoor/G2/hostile sites are read server-side (``r.jina.ai``) so the home IP
is never presented to their anti-bot. Advisory only: it scores and stores a
text snapshot but never hard-blocks a ticker (``advisory: true``).
"""

import re
from typing import Dict, List, Optional, Tuple

import requests

from discovery.sentinel import queue as q

_POSITIVE = re.compile(r"\b(strong|growth|leader|award|positive|best|great|excellent)\b", re.I)
_NEGATIVE = re.compile(r"\b(scam|fake|broken|fraud|layoff|decline|negative|terrible)\b", re.I)


def _score(text: str) -> float:
    if not text:
        return 0.0
    pos = len(_POSITIVE.findall(text))
    neg = len(_NEGATIVE.findall(text))
    denom = max(1, pos + neg)
    return round((pos - neg) / denom, 3)


def fetch_via_jina(url: str, jina_base: str, max_chars: int,
                   timeout: int = 60) -> Tuple[Optional[str], int]:
    target = f"{jina_base}/{url}"
    try:
        resp = requests.get(
            target,
            headers={"User-Agent": "Mozilla/5.0 (Sentinel enrichment lane)"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None, 0
    if resp.status_code != 200:
        return None, resp.status_code
    return resp.text[:max_chars], resp.status_code


def enrich_ticker(
    conn, ticker: str, targets: List[Tuple[str, str]], cfg: Dict,
) -> Dict[str, float]:
    """Fetch+store each (source, url) target for a ticker. Returns {source: score}."""
    g4 = cfg["gates"]["g4_enrich"]
    jina = g4["jina_base_url"]
    max_chars = g4["max_text_chars"]
    out = {}
    for source, url in targets:
        text, code = fetch_via_jina(url, jina, max_chars)
        if text is None:
            q.enrich_store(conn, ticker, source, url, None, None)
            out[source] = None
            continue
        sc = _score(text)
        q.enrich_store(conn, ticker, source, url, text, sc)
        out[source] = sc
    return out


def default_targets(ticker: str, companies: Dict) -> List[Tuple[str, str]]:
    """Build enrichment targets from hybrid_config companies slugs (best-effort)."""
    meta = companies.get(ticker, {})
    slugs = {
        "glassdoor": meta.get("glassdoor_slug"),
        "g2": meta.get("g2_slug"),
    }
    targets = []
    if slugs.get("glassdoor"):
        targets.append(("glassdoor", f"https://www.glassdoor.com/Reviews/{slugs['glassdoor']}-Reviews-E{len(slugs['glassdoor'])}.htm"))
    if slugs.get("g2"):
        targets.append(("g2", f"https://www.g2.com/products/{slugs['g2']}/reviews"))
    return targets
