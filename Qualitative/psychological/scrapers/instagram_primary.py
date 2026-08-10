"""Instagram anti-bot scraper for the independent discovery channel
(D-20260807-002, CEO APPROVED).

Adapts the Reddit/Glassdoor anti-bot toolkit (nodriver + CDP stealth) into a
cookie-authenticated Instagram session. The discovery feed stays a leaf:
``discovery.enabled`` remains false and nothing here touches portfolio,
backtesting or DB-write code. Fail-closed always: on any failure the code
raises (or deg-tags via the discovery wrapper) — it never invents data.

Module is import-safe offline: heavy toolkits (nodriver's cookie API,
curl_cffi, cdp_stealth) are imported lazily inside methods/functions, so tests
that never launch a browser can import this module cleanly.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import load_hybrid_config
from psychological.scrapers.nodriver_scraper import (
    NodriverSession,
    NodriverConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_HASHTAGS = [
    "semiconductors", "datacenter", "photonics", "liquidcooling",
    "advancedpackaging", "highnaeuv", "nvidia", "asml",
]

_DEFAULT_BULLISH = {
    "call": 1.0,
    "moon": 1.5,
    "long": 1.0,
    "tendies": 1.2,
    "diamond hands": 2.0,
    "yolo": 1.0,
    "undervalued": 1.0,
    "bullish": 1.5,
    "rip": 1.0,
    "green": 0.5,
}

_DEFAULT_BEARISH = {
    "put": 1.0,
    "crash": 1.5,
    "short": 1.0,
    "bagholder": 1.5,
    "paper hands": 2.0,
    "overvalued": 1.0,
    "dump": 1.2,
    "bearish": 1.5,
    "red": 0.5,
    "tank": 1.0,
}


class InstagramSessionUnavailable(RuntimeError):
    """The Instagram session is unusable (login wall, network refusal)."""


class InstagramCookieMissing(RuntimeError):
    """The Instagram session cookie file is missing (fail-closed gate)."""


class InstagramChallengeDetected(RuntimeError):
    """Instagram served an anti-bot challenge; stop, never brute-force."""


class InstagramCoolDown(RuntimeError):
    """Per-session page budget exhausted; session must cool down."""


@dataclass
class InstagramConfig:
    """Configuration for the Instagram anti-bot session."""

    headless: bool = True
    min_delay: float = 30.0
    max_delay: float = 60.0
    max_pages_per_session: int = 25
    session_cool_down_seconds: float = 180.0
    session_file: str = "config/instagram_cookies.json"
    sessions_dir: str = "config/instagram_sessions"
    proxies_file: str = "config/proxies.txt"
    app_id: str = "936619743392459"
    hashtags: List[str] = field(default_factory=lambda: list(_DEFAULT_HASHTAGS))
    finance_accounts: List[str] = field(default_factory=list)
    browser_executable_path: Optional[str] = None

    def __init__(self, config_dict: dict = None):
        cfg = config_dict
        if cfg is None:
            cfg = load_hybrid_config().get("psychological", {}).get("instagram", {})
        self.headless = bool(cfg.get("headless", True))
        self.min_delay = float(cfg.get("min_delay", 30.0))
        self.max_delay = float(cfg.get("max_delay", 60.0))
        self.max_pages_per_session = int(cfg.get("max_pages_per_session", 25))
        self.session_cool_down_seconds = float(cfg.get("session_cool_down_seconds", 180.0))
        self.session_file = str(cfg.get("session_file", "config/instagram_cookies.json"))
        self.sessions_dir = str(cfg.get("sessions_dir", "config/instagram_sessions"))
        self.proxies_file = str(cfg.get("proxies_file", "config/proxies.txt"))
        self.app_id = str(cfg.get("app_id", "936619743392459"))
        self.hashtags = list(cfg.get("hashtags", _DEFAULT_HASHTAGS))
        self.finance_accounts = list(cfg.get("finance_accounts", []))
        self.browser_executable_path = cfg.get("browser_executable_path")


_CHALLENGE_SIGNALS = [
    "we detected unusual activity",
    "accounts/challenge",
    "confirm you're human",
    "confirm your identity",
    "review your activity",
    "unusual traffic",
    "suspicious activity",
    "action=unfollow",
    "login.html?next",
    "challenge required",
    "temporary block",
]

_LOGIN_WALL_SIGNALS = [
    "log in to see",
    "please log in",
    "sign up to see",
    "login required",
    "profile.php",
]


def detect_instagram_challenge(html: str) -> bool:
    """True if ``html`` contains any Instagram anti-bot challenge signal."""
    if not html:
        return False
    lower = html.lower()
    return any(signal in lower for signal in _CHALLENGE_SIGNALS)


def detect_instagram_login_wall(html: str) -> bool:
    """True if ``html`` contains any Instagram login-wall signal."""
    if not html:
        return False
    lower = html.lower()
    return any(signal in lower for signal in _LOGIN_WALL_SIGNALS)


def _extract_json_object(html: str, marker: str) -> Optional[dict]:
    """Extract the first balanced JSON object after ``marker``, or None."""
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except Exception:  # noqa: BLE001 - malformed JSON -> None
                    return None
    return None


def _find_edges(entry_data: dict) -> List[dict]:
    """Walk ProfilePage/TagPage/top-level entry_data for media edges."""
    if not isinstance(entry_data, dict):
        return []
    profile = entry_data.get("ProfilePage")
    if isinstance(profile, list) and profile:
        user = (profile[0] or {}).get("graphql", {}).get("user", {})
        edges = (user or {}).get("edge_owner_to_timeline_media", {}).get("edges") or []
        if edges:
            return edges
    tag = entry_data.get("TagPage")
    if isinstance(tag, list) and tag:
        hashtag = (tag[0] or {}).get("graphql", {}).get("hashtag", {})
        edges = (hashtag or {}).get("edge_hashtag_to_media", {}).get("edges") or []
        if edges:
            return edges
    for page in entry_data.values():
        if isinstance(page, list) and page:
            graphql = (page[0] or {}).get("graphql") or {}
            for container in graphql.values():
                if isinstance(container, dict) and isinstance(container.get("edges"), list):
                    return container["edges"]
    return []


def _normalize_post(node: dict) -> dict:
    """Map one Instagram graphql media node to a discovery post dict."""
    caption = ""
    cap_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if cap_edges and isinstance(cap_edges[0], dict):
        caption = (cap_edges[0].get("node") or {}).get("text") or ""
    owner = node.get("owner") or {}
    likes = (node.get("edge_liked_by") or {}).get("count")
    if likes is None:
        likes = (node.get("edge_media_preview_like") or {}).get("count") or 0
    comments = (node.get("edge_media_to_comment") or {}).get("count") or 0
    views = node.get("video_view_count")
    if views is not None:
        try:
            views = int(views)
        except (TypeError, ValueError):
            views = None
    video_url = node.get("video_url") if node.get("is_video") else None
    return {
        "shortcode": str(node.get("shortcode") or ""),
        "caption": caption,
        "hashtags": re.findall(r"#(\w+)", caption),
        "likes": likes,
        "comments": comments,
        "views": views,
        "video_url": video_url,
        "author_username": str(owner.get("username") or ""),
        "author_followers": (owner.get("edge_followed_by") or {}).get("count") or 0,
        "author_verified": bool(owner.get("is_verified") or False),
    }


def parse_shared_data(html: str, limit: Optional[int] = None) -> List[dict]:
    """Parse Instagram embedded JSON (``_sharedData``/``__additionalDataLoaded``)
    into normalized post dicts. Returns [] on any parse failure."""
    if not html:
        return []
    data = _extract_json_object(html, "window._sharedData")
    if data is None:
        data = _extract_json_object(html, "__additionalDataLoaded")
    if data is None:
        return []
    posts = []
    if isinstance(data, list):
        posts = data
    else:
        edges = _find_edges(data.get("entry_data") or {})
        posts = [e.get("node") for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict)]
    out = []
    for node in posts:
        if not isinstance(node, dict):
            continue
        out.append(_normalize_post(node))
    if limit is not None:
        out = out[:limit]
    return out


def _normalize_web_info_item(item: dict) -> dict:
    caption_obj = item.get("caption")
    caption = ""
    if isinstance(caption_obj, dict):
        caption = caption_obj.get("text") or ""
    user_obj = item.get("user") or {}
    
    likes = item.get("like_count")
    if likes is None:
        likes = 0
    comments = item.get("comment_count")
    if comments is None:
        comments = 0
    views = item.get("view_count")
    if views is None:
        views = item.get("video_view_count")
    if views is not None:
        try:
            views = int(views)
        except (TypeError, ValueError):
            views = None
            
    video_url = None
    if item.get("is_video") or item.get("media_type") == 2:
        video_versions = item.get("video_versions")
        if isinstance(video_versions, list) and video_versions:
            video_url = video_versions[0].get("url")
        else:
            video_url = item.get("video_url")
            
    return {
        "shortcode": str(item.get("code") or ""),
        "caption": caption,
        "hashtags": re.findall(r"#(\w+)", caption),
        "likes": likes,
        "comments": comments,
        "views": views,
        "video_url": video_url,
        "author_username": str(user_obj.get("username") or ""),
        "author_followers": 0,
        "author_verified": bool(user_obj.get("is_verified") or False),
    }


def parse_embedded_media_json(html: str, limit: Optional[int] = None) -> List[dict]:
    if not html:
        return []
    
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL)
    
    def walk_json(obj, hits: List[dict]):
        if isinstance(obj, dict):
            if "xdt_api__v1__media__shortcode__web_info" in obj:
                web_info = obj["xdt_api__v1__media__shortcode__web_info"]
                if isinstance(web_info, dict):
                    items = web_info.get("items")
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                hits.append(item)
            for v in obj.values():
                walk_json(v, hits)
        elif isinstance(obj, list):
            for item in obj:
                walk_json(item, hits)

    raw_items = []
    for content in scripts:
        content_stripped = content.strip()
        if not content_stripped:
            continue
        try:
            parsed = json.loads(content_stripped)
            walk_json(parsed, raw_items)
        except Exception:
            continue

    seen = set()
    normalized_posts = []
    for item in raw_items:
        post = _normalize_web_info_item(item)
        sc = post["shortcode"]
        if sc and sc not in seen:
            seen.add(sc)
            normalized_posts.append(post)

    if limit is not None:
        normalized_posts = normalized_posts[:limit]
    return normalized_posts


def _unwrap_nodriver(v):
    if isinstance(v, dict):
        if "type" in v and "value" in v:
            val = v["value"]
            if v["type"] == "object" and isinstance(val, list):
                res = {}
                for item in val:
                    if isinstance(item, list) and len(item) == 2:
                        k, val_item = item
                        res[str(k)] = _unwrap_nodriver(val_item)
                return res
            elif isinstance(val, (dict, list)):
                return _unwrap_nodriver(val)
            else:
                return val
        else:
            return {k: _unwrap_nodriver(val) for k, val in v.items()}
    elif isinstance(v, list):
        return [_unwrap_nodriver(item) for item in v]
    else:
        return v



_COMMON_WORDS = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HAS", "HAD", "WAS", "ONE", "TWO", "NEW", "OLD", "SEE", "GET", "GOT", "LET", "PUT", "CALL", "LONG", "SHORT", "MOON", "CRASH", "PUMP", "DUMP", "RIP", "YTD", "CEO", "CFO", "CTO", "IPO", "ETF", "SEC", "FDA", "FOMC", "CPI", "PPI", "GDP", "EPS", "PE", "ROE", "ROA", "EBITDA", "FCF", "DCF", "AI", "ML", "GPU", "CPU", "API", "SDK", "UI", "UX", "DB", "SQL", "AWS", "GCP", "K8S", "CI", "CD", "PR", "QA", "DEV", "OPS", "SRE", "PM", "PO", "CTO", "VP", "DIR", "MGR", "ENG", "TECH", "SALES", "HR", "IT", "FIN", "OPS", "MKT", "BD", "R&D", "Q1", "Q2", "Q3", "Q4", "FY", "TTM", "YOY", "QOQ", "MOM", "WOW", "DOD", "AH", "PM", "AM", "EST", "PST", "CST", "MST", "UTC", "GMT", "EDT", "PDT", "CDT", "MDT",
    "WHAT", "MOVES", "YOUR", "JUNE", "THIS", "THAT", "WITH", "FROM", "HAVE", "BEEN", "WERE", "THEY", "THEIR", "THERE", "THEN", "THAN", "WHEN", "WHERE", "WHICH", "WHO", "WHOM", "WHOSE", "WHY", "HOW", "ITS", "OUR", "OUT", "OVER", "OWN", "SAME", "SUCH", "VERY", "WELL", "WILL", "WOULD", "ABOUT", "AFTER", "AGAIN", "BELOW", "COULD", "EVERY", "FIRST", "FOUND", "GREAT", "GROUP", "HAND", "HIGH", "HOME", "LARGE", "LAST", "LEFT", "LIFE", "LIGHT", "LIKE", "LINE", "LITTLE", "LONG", "LOOK", "MADE", "MAKE", "MAN", "MANY", "MAY", "MIGHT", "MOST", "MUST", "NEVER", "NEXT", "NIGHT", "ONLY", "OPEN", "ORDER", "OTHER", "PART", "PLACE", "POINT", "POWER", "PUBLIC", "RIGHT", "SAID", "SAME", "SAW", "SAY", "SEE", "SEEM", "SEEN", "SHALL", "SHOULD", "SHOW", "SIDE", "SINCE", "SMALL", "SOUND", "STILL", "STUDY", "SYSTEM", "TAKE", "TELL", "THOSE", "THOUGH", "THOUGHT", "THROUGH", "THUS", "TOGETHER", "TOO", "TOOK", "TURN", "UNDER", "UNTIL", "UPON", "USED", "USES", "USING", "USUALLY", "VARIOUS", "WANT", "WAY", "WAYS", "WEEK", "WEEKS", "WENT", "WHERE", "WHILE", "WHITE", "WHOLE", "WITHIN", "WITHOUT", "WORK", "WORLD", "YEAR", "YEARS", "YOUNG", "ZUCK", "KOSPI", "SPCX", "TOP", "BRACE", "DUDE", "HOPE", "WELL", "DOING", "RETIREMENT", "ACCOUNT", "GROUND", "ONLY", "THESE", "WERE", "PUTS", "IF", "ALL", "IN", "ON", "USD", "ZUCK", "BRACE", "KOSPI", "SPCX", "TOP", "RIP",
    "TO", "IS", "IT", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "HI", "IF", "IN", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "UP", "US", "WE", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "HI", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE", "HIS", "HER", "HIM", "SHE", "THEM", "THEN", "THAN", "THAT", "THIS", "THOSE", "THESE", "THERE", "WHERE", "WHEN", "WHY", "HOW", "WHO", "WHOM", "WHOSE", "WHICH", "WHAT", "WHICH", "WHILE", "WITH", "WITHIN", "WITHOUT", "YOUR", "YOU", "YOURS", "OUR", "OURS", "MY", "MINE", "HIS", "HERS", "ITS", "THEIR", "THEIRS",
}


_TICKER_UNIVERSE_CACHE = None


def _real_ticker_universe() -> Optional[set]:
    """Real SEC-registered ticker symbols, cached from the local CIK map.

    Instagram prose is full of ALL-CAPS English words (BUY, BEAR, HOLD...), so
    the blacklist alone is not enough: candidates are whitelisted against the
    SEC company-tickers universe. Returns None when the map is unavailable so
    callers fall back to blacklist-only (still never fabricates). Crypto tokens
    (BTC/ETH/...) are intentionally absent: this is a stock pipeline and the
    stock screen would reject them anyway.
    """
    global _TICKER_UNIVERSE_CACHE
    if _TICKER_UNIVERSE_CACHE is not None:
        return _TICKER_UNIVERSE_CACHE
    try:
        from valuation_alpha.universe.cik_resolver import get_cik_map

        mapping = get_cik_map()
        universe = set(mapping) if mapping else None
    except Exception:  # noqa: BLE001 - degrade to blacklist-only
        universe = None
    _TICKER_UNIVERSE_CACHE = universe
    return universe


_TECH_KEYWORD_MAP = {
    "co-packaged optics": ["FN", "CLS", "LITE", "COHR"],
    "silicon photonics": ["FN", "CLS", "LITE", "COHR"],
    "liquid cooling": ["VRT", "MOD", "VICR"],
    "high-na euv": ["ASML", "LRCX", "AMAT", "KLAC"],
    "hbm3e": ["MU", "AVGO"],
    "advanced packaging": ["TSM", "ASX", "AMAT"],
    "euv pellicle": ["ASML", "LRCX"],
    "wafer testing": ["TER", "COHU", "ONTO", "NVMI"],
}


def llm_analyze_transcript_buzzwords(transcript: str) -> List[str]:
    """Execute a semantic analysis of the transcript using the opencode CLI.
    Identify ground-breaking scientific breakthroughs, AI tech, and map them to supplier tickers.
    Falls back to static keyword extraction on failure.
    """
    if not transcript:
        return []
    import subprocess
    import json
    import re
    
    prompt = (
        "Analyze the following transcript from a video. Identify if it discusses any "
        "ground-breaking scientific discoveries, biotech breakthroughs, advanced AI developments, "
        "semiconductor technologies, or deep tech. If it does, map the technologies mentioned "
        "to the public stock tickers of the small-cap or mid-cap component/subsystem suppliers (e.g., Vertiv VRT, "
        "Celestica CLS, Fabrinet FN, Micron MU, Broadcom AVGO, ASML, Lam Research LRCX). "
        "Return ONLY a JSON list of stock tickers, like [\"VRT\", \"CLS\"]. Return [] if none fit.\n"
        f"Transcript: {transcript}"
    )
    try:
        result = subprocess.run(
            ["opencode", "run", prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
        )
        if result.returncode == 0 and result.stdout:
            match = re.search(r"\[\s*\"[A-Z0-9,\s\"]*\"\s*\]", result.stdout)
            if match:
                tickers = json.loads(match.group(0))
                return [t.upper().strip() for t in tickers if isinstance(t, str)]
    except Exception:
        pass
    
    tickers = set()
    lower_text = transcript.lower()
    for kw, mapped in _TECH_KEYWORD_MAP.items():
        if kw in lower_text:
            for t in mapped:
                tickers.add(t)
    return list(tickers)


def extract_tickers(text: str, llm_tickers: List[str] = None) -> List[str]:
    """Extract uppercase ticker candidates from ``text``, including keyword-mapped suppliers.

    Blacklist approach plus a real-ticker whitelist when the local SEC CIK map is
    available. Also scans for technical keywords (e.g. "liquid cooling") and maps
    them to their sub-system suppliers (VRT, CLS) to bypass megacap buyer bias.
    """
    if not text:
        return []
    from config.constants import TICKER_BLACKLIST

    blacklist = set(TICKER_BLACKLIST) | _COMMON_WORDS
    universe = _real_ticker_universe()
    tickers = set()
    
    if llm_tickers:
        for t in llm_tickers:
            tickers.add(t)
            
    # 1. Scan for technical keyword mapping
    lower_text = text.lower()
    for kw, mapped in _TECH_KEYWORD_MAP.items():
        if kw in lower_text:
            for t in mapped:
                tickers.add(t)
                
    # 2. Traditional regex parser
    words = re.findall(r"\b[A-Z]{1,5}\b", text.upper())
    for word in words:
        if len(word) < 2 or word in blacklist:
            continue
        if universe is not None and word not in universe:
            continue
        tickers.add(word)
    if "INTC" in text.upper() or "INTEL" in text.upper():
        if re.search(r"\bINTC\b", text):
            tickers.add("INTC")
        elif "INTEL" in text.upper():
            if not re.search(r"\b(intel inside|intel core|intel processor|intel arc)\b", text, re.IGNORECASE):
                tickers.add("INTC")
    return list(tickers)


def compute_sentiment(text: str) -> Optional[float]:
    """Deterministic lexicon sentiment: bullish terms add, bearish subtract.
    Returns None when no lexicon term matches (no fabrication)."""
    if not text:
        return None
    lexicon = load_hybrid_config().get("psychological", {}).get("instagram", {}).get("sentiment_lexicon", {})
    bullish = lexicon.get("bullish", _DEFAULT_BULLISH)
    bearish = lexicon.get("bearish", _DEFAULT_BEARISH)
    lower = text.lower()
    score = 0.0
    matched = False
    for term, val in bullish.items():
        if term in lower:
            score += val
            matched = True
    for term, val in bearish.items():
        if term in lower:
            score -= val
            matched = True
    return score if matched else None


def transcribe_video_audio(video_url: str) -> str:
    """Download video_url, extract audio via ffmpeg, transcribe via Whisper.
    Falls back to "" on any error/missing dependencies (no-fail invariant).
    """
    if not video_url:
        return ""
    import tempfile
    import subprocess
    import urllib.request
    
    try:
        import whisper
    except ImportError:
        logger.warning("Whisper library not installed; skipping audio transcription.")
        return ""
        
    temp_dir = tempfile.gettempdir()
    video_path = os.path.join(temp_dir, "temp_ig_video.mp4")
    audio_path = os.path.join(temp_dir, "temp_ig_audio.wav")
    
    try:
        # Download video
        urllib.request.urlretrieve(video_url, video_path)
        
        # Extract audio using ffmpeg (mono, 16kHz WAV format)
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ar", "16000", "-ac", "1", "-f", "wav", audio_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode != 0:
            logger.warning("ffmpeg audio extraction failed.")
            return ""
            
        # Transcribe using Whisper
        model = whisper.load_model("base")
        transcription = model.transcribe(audio_path)
        text = transcription.get("text", "")
        return text
    except Exception as e:
        logger.warning(f"Audio transcription failed: {e}")
        return ""
    finally:
        for p in (video_path, audio_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def to_mention_row(post: dict, fetch_ts: int) -> List[dict]:
    """Map a post dict to discovery-compatible mention rows (one per ticker).

    Sanitizer fields ride along as extra keys (harmless to the wrapper).
    Returns [] for a post with no tickers.
    """
    caption = post.get("caption") or ""
    video_url = post.get("video_url")
    llm_tickers = None
    if video_url:
        transcription = transcribe_video_audio(video_url)
        if transcription:
            caption = caption + " " + transcription
            llm_tickers = llm_analyze_transcript_buzzwords(transcription)
            
    tickers = extract_tickers(caption, llm_tickers=llm_tickers)
    if not tickers:
        return []
    sentiment = compute_sentiment(caption)
    rows = []
    for ticker in tickers:
        rows.append({
            "entity": ticker,
            "topic": "Stocks",
            "fetch_ts": fetch_ts,
            "source_confidence": 0.6,
            "volume_or_rank": post.get("views") if post.get("views") else post.get("likes", 0),
            "sentiment": sentiment,
            "external_id": "https://www.instagram.com/p/{}/".format(post.get("shortcode") or ""),
            "caption": caption,
            "hashtags": post.get("hashtags", []),
            "comments": post.get("comments", 0),
            "views": post.get("views"),
            "followers": post.get("author_followers", 0),
            "verified": post.get("author_verified", False),
            "brand_account": bool(post.get("link_in_bio")) or bool(
                set(h.lower() for h in post.get("hashtags", [])) & {"ad", "sponsored", "spon", "partner"}
            ),
        })
    return rows


class InstagramSession:
    """Cookie-authenticated Instagram browser session (wraps NodriverSession).

    Fail-closed: warm-up raises on challenge/login-wall and the per-session
    page budget raises InstagramCoolDown instead of hammering Instagram.
    """

    def __init__(self, config: InstagramConfig = None):
        self.config = config or InstagramConfig()
        self._session: Optional[NodriverSession] = None
        self._pages_loaded = 0

    async def initialize(self) -> None:
        self._session = NodriverSession(NodriverConfig(
            headless=self.config.headless,
            browser_executable_path=self.config.browser_executable_path,
            min_delay=self.config.min_delay,
            max_delay=self.config.max_delay,
        ))
        await self._session.initialize()
        await self._session.apply_cdp_stealth()
        await self.load_cookies()
        await self._session.get("https://www.instagram.com/")
        html = await self._session.get_content()
        if detect_instagram_challenge(html):
            raise InstagramChallengeDetected("Instagram challenge detected during warm-up")
        if detect_instagram_login_wall(html):
            raise InstagramSessionUnavailable("login wall (cookies expired?)")
        self._pages_loaded = 0

    async def load_cookies(self) -> bool:
        """Load session cookies from ``config.session_file`` (list of dicts).

        Missing file -> False (caller decides). Unsupported cookie API -> log
        and continue, never crash. Returns True when the file was present.
        """
        path = self.config.session_file
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r") as f:
                cookies = json.load(f)
            tab = self._session.get_tab() if self._session else None
            jar = getattr(tab, "cookies", None) if tab is not None else None
            if jar is None:
                browser = getattr(self._session, "_browser", None) if self._session else None
                jar = getattr(browser, "cookies", None) if browser is not None else None
            if jar is None:
                logger.warning("nodriver cookie API unavailable; skipping cookie load")
                return True
            from nodriver.cdp.network import CookieParam, TimeSinceEpoch

            params = []
            for c in cookies:
                exp = c.get("expires")
                params.append(CookieParam(
                    name=str(c.get("name", "")),
                    value=str(c.get("value", "")),
                    domain=c.get("domain"),
                    path=c.get("path"),
                    secure=bool(c.get("secure")),
                    expires=TimeSinceEpoch(float(exp)) if exp else None,
                ))
            await jar.set_all(params)
            return True
        except Exception as e:  # noqa: BLE001 - never crash cookie load
            logger.warning("Failed to load Instagram cookies: %s", e)
            return True

    async def save_cookies(self) -> None:
        """Persist current browser cookies to ``config.session_file``. Never
        raises; logs and continues on any failure."""
        try:
            tab = self._session.get_tab() if self._session else None
            jar = getattr(tab, "cookies", None) if tab is not None else None
            if jar is None:
                browser = getattr(self._session, "_browser", None) if self._session else None
                jar = getattr(browser, "cookies", None) if browser is not None else None
            if jar is None:
                logger.warning("nodriver cookie API unavailable; skipping cookie save")
                return
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
            path = self.config.session_file
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f, indent=2)
        except Exception as e:  # noqa: BLE001 - never crash cookie save
            logger.warning("Failed to save Instagram cookies: %s", e)

    async def fetch_hashtag_posts(self, tag: str, limit: int) -> List[dict]:
        """Fetch posts for one hashtag page; raises on challenge/cool-down."""
        if self._pages_loaded >= self.config.max_pages_per_session:
            raise InstagramCoolDown("Instagram page budget exhausted")
        url = "https://www.instagram.com/explore/tags/{}/".format(tag)
        await self._session.get(url)
        self._pages_loaded += 1
        await asyncio.sleep(random.uniform(2, 4))
        html = await self._session.get_content()
        if detect_instagram_challenge(html):
            raise InstagramChallengeDetected("Instagram challenge detected on #{}".format(tag))
        if detect_instagram_login_wall(html):
            raise InstagramSessionUnavailable("login wall on #{}".format(tag))
        posts = parse_shared_data(html)
        if posts:
            return posts[:limit] if limit else posts

        js_code = 'Array.from(document.querySelectorAll(\'a[href*="/p/"]\')).map(a => a.getAttribute(\'href\'))'
        val = await self._session.evaluate(js_code)
        unwrapped = _unwrap_nodriver(val)
        if not unwrapped or not isinstance(unwrapped, list):
            return []

        shortcodes = []
        for href in unwrapped:
            if not isinstance(href, str):
                continue
            match = re.search(r'/p/([^/]+)', href)
            if match:
                sc = match.group(1)
                if sc not in shortcodes:
                    shortcodes.append(sc)

        cap = limit if limit else 12
        shortcodes = shortcodes[:cap]

        collected = []
        for sc in shortcodes:
            if len(collected) >= cap:
                break
            if self._pages_loaded >= self.config.max_pages_per_session:
                raise InstagramCoolDown("Instagram page budget exhausted")
            post_url = "https://www.instagram.com/p/{}/".format(sc)
            await self._session.get(post_url)
            self._pages_loaded += 1
            await asyncio.sleep(random.uniform(1.5, 3))
            post_html = await self._session.get_content()
            if detect_instagram_challenge(post_html):
                raise InstagramChallengeDetected("Instagram challenge detected on post {}".format(sc))
            if detect_instagram_login_wall(post_html):
                raise InstagramSessionUnavailable("login wall on post {}".format(sc))
            
            post_data = parse_embedded_media_json(post_html)
            for p in post_data:
                if p["shortcode"] == sc:
                    collected.append(p)
                    break
            else:
                if post_data:
                    collected.append(post_data[0])

        return collected[:limit] if limit else collected

    async def fetch_reels(self, limit: int) -> List[dict]:
        """Fetch reels page posts; reels schema may differ so parse defensively."""
        if self._pages_loaded >= self.config.max_pages_per_session:
            raise InstagramCoolDown("Instagram page budget exhausted")
        await self._session.get("https://www.instagram.com/reels/")
        self._pages_loaded += 1
        await asyncio.sleep(random.uniform(2, 4))
        html = await self._session.get_content()
        if detect_instagram_challenge(html):
            raise InstagramChallengeDetected("Instagram challenge detected on reels")
        if detect_instagram_login_wall(html):
            raise InstagramSessionUnavailable("login wall on reels")
        try:
            posts = parse_shared_data(html)
        except Exception:  # noqa: BLE001 - reels schema may differ
            return []
        return posts[:limit] if limit else posts

    async def fetch_profile_posts(self, username: str, limit: int) -> List[dict]:
        """Fetch one profile page's posts (ProfilePage parse path)."""
        if self._pages_loaded >= self.config.max_pages_per_session:
            raise InstagramCoolDown("Instagram page budget exhausted")
        url = "https://www.instagram.com/{}/".format(username)
        await self._session.get(url)
        self._pages_loaded += 1
        await asyncio.sleep(random.uniform(2, 4))
        html = await self._session.get_content()
        if detect_instagram_challenge(html):
            raise InstagramChallengeDetected("Instagram challenge detected on @{}".format(username))
        if detect_instagram_login_wall(html):
            raise InstagramSessionUnavailable("login wall on @{}".format(username))
        posts = parse_shared_data(html)
        return posts[:limit] if limit else posts

    async def close(self) -> None:
        try:
            await self.save_cookies()
        except Exception:  # noqa: BLE001 - cleanup must not raise
            pass
        if self._session:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                pass
            self._session = None

    async def __aenter__(self) -> "InstagramSession":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def _fetch_mentions_async(limit: int, config: InstagramConfig) -> List[dict]:
    rows: List[dict] = []
    async with InstagramSession(config) as session:
        for tag in config.hashtags:
            if len(rows) >= limit:
                break
            posts = await session.fetch_hashtag_posts(tag, limit=limit - len(rows))
            for post in posts:
                rows.extend(to_mention_row(post, fetch_ts=int(time.time())))
                if len(rows) >= limit:
                    break
        for username in config.finance_accounts:
            if len(rows) >= limit:
                break
            posts = await session.fetch_profile_posts(username, limit=limit - len(rows))
            for post in posts:
                rows.extend(to_mention_row(post, fetch_ts=int(time.time())))
                if len(rows) >= limit:
                    break
    return rows[:limit]


def _get_random_proxy(proxies_file: str) -> Optional[str]:
    """Load and return a random proxy from proxies_file, or None."""
    if not os.path.exists(proxies_file):
        return None
    try:
        with open(proxies_file, "r") as f:
            proxies = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
        return random.choice(proxies) if proxies else None
    except Exception:
        return None


def _resolve_session_path(config: InstagramConfig) -> str:
    """Resolve cookie file path from default or pick a random one from sessions_dir."""
    if os.path.exists(config.session_file):
        return config.session_file
    if os.path.exists(config.sessions_dir):
        import glob
        files = glob.glob(os.path.join(config.sessions_dir, "*.json"))
        if files:
            return random.choice(files)
    return config.session_file


def _read_cookie_header(path: str, config: Optional[InstagramConfig] = None) -> str:
    """Build a Cookie header from the session cookie file, or ""."""
    resolved_path = path
    if config:
        resolved_path = _resolve_session_path(config)
    elif not os.path.exists(path) and os.path.exists("config/instagram_sessions"):
        import glob
        files = glob.glob("config/instagram_sessions/*.json")
        if files:
            resolved_path = random.choice(files)
            
    if not os.path.exists(resolved_path):
        return ""
    try:
        with open(resolved_path, "r") as f:
            cookies = json.load(f)
        return "; ".join(
            "{}={}".format(c.get("name"), c.get("value"))
            for c in cookies if c.get("name") and c.get("value")
        )
    except Exception:  # noqa: BLE001 - fallback must never raise
        return ""


_IG_MOBILE_UA = (
    "Instagram 265.0.0.23.301 Android (28/9; 420dpi; 1080x1920; "
    "samsung; SM-G960U; en_US; 265.0.0.23.301)"
)


def _decode_shortcode(shortcode: str) -> str:
    """Convert an Instagram shortcode to its numeric media id (base64)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    n = 0
    for ch in shortcode:
        n = n * 64 + alphabet.index(ch)
    return str(n)


def _normalize_private_api_item(item: dict) -> dict:
    """Map one private-api media dict (tags/sections or media/info) to the
    discovery post dict shape used by ``to_mention_row``."""
    caption = ""
    cap = item.get("caption")
    if isinstance(cap, dict):
        caption = cap.get("text") or ""
    elif isinstance(cap, str):
        caption = cap
    user = item.get("user") or {}
    views = item.get("view_count")
    if views is None:
        views = item.get("video_view_count")
    video_url = None
    if item.get("media_type") == 2 or item.get("is_video"):
        video_versions = item.get("video_versions")
        if isinstance(video_versions, list) and video_versions:
            video_url = video_versions[0].get("url")
        else:
            video_url = item.get("video_url")
    return {
        "shortcode": str(item.get("code") or ""),
        "caption": caption,
        "hashtags": re.findall(r"#(\w+)", caption),
        "likes": item.get("like_count") or 0,
        "comments": item.get("comment_count") or 0,
        "views": views,
        "video_url": video_url,
        "author_username": str(user.get("username") or ""),
        "author_followers": 0,
        "author_verified": bool(user.get("is_verified") or False),
    }


def _fetch_clips_home(limit: int, config: InstagramConfig) -> List[dict]:
    """Scrape Instagram personalized Reels (clips/home) to gather randomized,
    genre-specific videos using rotated profiles and IP proxies.

    Bypasses standard hashtag search and pulls recommended content.
    Uses irregular delay intervals and heavy stealth request headers.
    """
    try:
        from curl_cffi.requests import Session
    except ImportError:
        return []
    cookie_header = _read_cookie_header(config.session_file, config)
    if not cookie_header:
        return []

    connection_types = ["WIFI", "CELLULAR", "EXCELLENT"]
    locales = ["en_US", "en_GB", "es_ES", "fr_FR"]
    headers = {
        "User-Agent": _IG_MOBILE_UA,
        "X-Requested-With": "XMLHttpRequest",
        "X-IG-App-ID": config.app_id,
        "X-IG-Capabilities": "36r/Fx8=",
        "X-IG-Connection-Type": random.choice(connection_types),
        "X-IG-App-Locale": random.choice(locales),
        "Accept": "*/*",
        "Cookie": cookie_header,
    }

    proxy = _get_random_proxy(config.proxies_file)
    proxies_dict = {"http": proxy, "https": proxy} if proxy else None

    rows: List[dict] = []
    try:
        with Session(impersonate="chrome", proxies=proxies_dict) as session:
            max_id = ""
            # Dynamically size the number of chunks to fetch so we can meet the requested limit
            num_chunks = max(5, int(limit / 6) + 2)
            chunks = [random.randint(4, 10) for _ in range(num_chunks)]
            for idx, count in enumerate(chunks):
                if len(rows) >= limit:
                    break
                try:
                    # Optimized micro-delay with human-like jitter
                    delay = random.uniform(2.0, 7.0)
                    time.sleep(delay)

                    # Simulate random human session breaks mid-batch (15% chance to pause)
                    if idx > 0 and random.random() < 0.15:
                        mid_break = random.randint(30, 120)
                        logger.info("Simulating human pause for %ds...", mid_break)
                        time.sleep(mid_break)

                    resp = session.post(
                        "https://i.instagram.com/api/v1/clips/home/",
                        headers=headers,
                        data={"max_id": max_id, "count": str(count)},
                        timeout=20,
                    )
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    payload = resp.json()
                except Exception:
                    continue

                posts = []
                for item in payload.get("items") or []:
                    media = item.get("media") if isinstance(item, dict) else None
                    if isinstance(media, dict):
                        posts.append(_normalize_private_api_item(media))

                for post in posts:
                    rows.extend(to_mention_row(post, fetch_ts=int(time.time())))
                    if len(rows) >= limit:
                        break

                max_id = payload.get("next_max_id") or ""
                if not max_id:
                    break
        return rows[:limit]
    except Exception:
        return []


def _fetch_private_api(limit: int, config: InstagramConfig) -> List[dict]:
    """Browser-free Instagram private API fetch (validated Aug 2026).

    Primary path: POST i.instagram.com/api/v1/tags/{tag}/sections/ with the
    mobile app UA + web app id + session cookies. Never raises; returns []
    on any failure so callers fall through to the browser path.
    """
    try:
        from curl_cffi.requests import Session
    except ImportError:
        return []
    cookie_header = _read_cookie_header(config.session_file, config)
    if not cookie_header:
        return []
    headers = {
        "User-Agent": _IG_MOBILE_UA,
        "X-Requested-With": "XMLHttpRequest",
        "X-IG-App-ID": config.app_id,
        "Accept": "*/*",
        "Cookie": cookie_header,
    }
    proxy = _get_random_proxy(config.proxies_file)
    proxies_dict = {"http": proxy, "https": proxy} if proxy else None
    rows: List[dict] = []
    try:
        with Session(impersonate="chrome", proxies=proxies_dict) as session:
            for tag in config.hashtags:
                if len(rows) >= limit:
                    break
                try:
                    resp = session.post(
                        "https://i.instagram.com/api/v1/tags/{}/sections/".format(tag),
                        headers=headers,
                        data={"tab": "recent", "count": "30", "max_posts": ""},
                        timeout=20,
                    )
                except Exception:  # noqa: BLE001 - per-tag failure, continue
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    payload = resp.json()
                except Exception:  # noqa: BLE001 - bad body, continue
                    continue
                posts: List[dict] = []
                for sec in payload.get("sections") or []:
                    layout = (sec or {}).get("layout_content") or {}
                    for m in layout.get("medias") or []:
                        media = m.get("media") if isinstance(m, dict) else None
                        if isinstance(media, dict):
                            posts.append(_normalize_private_api_item(media))
                for post in posts:
                    rows.extend(to_mention_row(post, fetch_ts=int(time.time())))
                    if len(rows) >= limit:
                        break
                time.sleep(config.min_delay)
        return rows[:limit]
    except Exception:  # noqa: BLE001 - fallback must never raise
        return []


def _curl_cffi_fallback(limit: int, config: InstagramConfig) -> List[dict]:
    """Legacy curl_cffi fallback (corp_audit ladder style). Never raises;
    returns [] on any failure so the caller re-raises the original error."""
    try:
        from curl_cffi.requests import Session
        from psychological.scrapers.cdp_stealth import random_user_agent
    except ImportError:
        return []
    try:
        headers = {
            "User-Agent": random_user_agent(),
            "X-Requested-With": "XMLHttpRequest",
            "X-IG-App-ID": config.app_id,
        }
        cookie_header = _read_cookie_header(config.session_file, config)
        if cookie_header:
            headers["Cookie"] = cookie_header
        proxy = _get_random_proxy(config.proxies_file)
        proxies_dict = {"http": proxy, "https": proxy} if proxy else None
        rows: List[dict] = []
        with Session(proxies=proxies_dict) as session:
            for tag in config.hashtags:
                if len(rows) >= limit:
                    break
                resp = session.get(
                    "https://www.instagram.com/explore/tags/{}/".format(tag),
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue
                posts = parse_shared_data(resp.text)
                if not posts:
                    posts = parse_embedded_media_json(resp.text)
                for post in posts:
                    rows.extend(to_mention_row(post, fetch_ts=int(time.time())))
                    if len(rows) >= limit:
                        break
                time.sleep(config.min_delay)
        return rows[:limit]
    except Exception:  # noqa: BLE001 - fallback must never raise
        return []


def fetch_instagram_mentions(limit: int = 100, config: InstagramConfig = None) -> List[dict]:
    """SYNC entrypoint for the discovery layer.

    Fail-closed gate: the session cookie file must exist BEFORE any browser is
    launched, so offline tests exercise the cookie check without a browser.
    Primary path is the browser-free private API; the browser path is the
    secondary (DOM extraction); curl_cffi is the last resort. Challenge /
    cool-down / login-wall re-raise; other failures fall back; if the fallback
    also fails the original error is re-raised.
    """
    cfg = config or InstagramConfig()
    if not os.path.exists(cfg.session_file):
        raise InstagramCookieMissing(
            "Instagram session cookie missing; export from a logged-in browser "
            "to config/instagram_cookies.json (git-ignored)"
        )
    clips_rows = _fetch_clips_home(limit, cfg)
    if clips_rows:
        return clips_rows
    api_rows = _fetch_private_api(limit, cfg)
    if api_rows:
        return api_rows
    try:
        return asyncio.run(_fetch_mentions_async(limit, cfg))
    except (InstagramChallengeDetected, InstagramCoolDown, InstagramSessionUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - fallback, then re-raise original
        fallback = _curl_cffi_fallback(limit, cfg)
        if fallback:
            return fallback
        raise
