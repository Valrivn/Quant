TICKER_BLACKLIST = {
    "A", "I", "DD", "YOLO", "FOMO", "ATH", "CEO", "IPO", "ETF", "FED", 
    "GDP", "IRS", "SEC", "EST", "EDT", "PST", "PDT", "USA", "UK", "EU",
    "US", "AI", "GPU", "CPU", "RAM", "VRAM", "API", "R&D", "DCF", "FCF",
    "ROIC", "WACC", "CAPM", "MRP", "YTM", "CPI", "PCE", "PE", "PS", "PB"
}

VALIDATION_KEYWORDS = {
    "shares", "share", "stock", "stocks", "calls", "call", "puts", "put", 
    "options", "option", "buy", "sell", "buying", "selling", "long", "short",
    "portfolio", "position", "positions", "dividend", "dividends", "yield",
    "market", "bull", "bear", "valuation", "ticker", "earnings", "revenue"
}

FINANCIAL_LEXICON = {
    "bullish": 2.0,
    "calls": 1.5,
    "moon": 2.0,
    "tendies": 1.5,
    "yolo": 1.0,
    "undervalued": 1.5,
    "growth": 1.0,
    "moat": 1.5,
    "puts": -1.5,
    "bearish": -2.0,
    "bagholder": -1.5,
    "rug pull": -2.0,
    "dump": -1.5,
    "short": -0.5,
    "overvalued": -1.5,
    "loss": -1.0,
    "bankrupt": -2.5
}

ENTITY_RESOLUTION = {
    "apple": "AAPL", "microsoft": "MSFT", "amazon": "AMZN", "google": "GOOGL", "alphabet": "GOOGL",
    "meta": "META", "facebook": "META", "tesla": "TSLA", "nvidia": "NVDA", "netflix": "NFLX",
    "amd": "AMD", "intel": "INTC", "qualcomm": "QCOM", "broadcom": "AVGO", "avgo": "AVGO",
    "jpmorgan": "JPM", "jpm": "JPM", "bank of america": "BAC", "wells fargo": "WFC",
    "goldman sachs": "GS", "morgan stanley": "MS", "citigroup": "C", "blackrock": "BLK",
    "berkshire": "BRK.B", "brk": "BRK.B", "brk.b": "BRK.B",
    "gamestop": "GME", "game stop": "GME", "amc": "AMC", "amc entertainment": "AMC",
    "bed bath beyond": "BBBY", "bbby": "BBBY", "blackberry": "BB", "bb": "BB",
    "nokia": "NOK", "nok": "NOK", "express": "EXPR", "koss": "KOSS",
    "tsmc": "TSM", "taiwan semiconductor": "TSM", "asml": "ASML", "asml holding": "ASML",
    "lattice": "LSCC", "marvell": "MRVL", "micron": "MU", "skyworks": "SWKS",
    "rivian": "RIVN", "lucid": "LCID", "nio": "NIO", "xpeng": "XPEV", "li auto": "LI",
    "plug power": "PLUG", "fuelcell": "FCEL", "blink": "BLNK", "chargepoint": "CHPT",
    "moderna": "MRNA", "pfizer": "PFE", "biontech": "BNTX", "novavax": "NVAX",
    "gilead": "GILD", "amgen": "AMGN", "regeneron": "REGN", "vertex": "VRTX",
    "coinbase": "COIN", "microstrategy": "MSTR", "mstr": "MSTR", "riot": "RIOT",
    "marathon": "MARA", "marathon digital": "MARA", "hut 8": "HUT",
    "alibaba": "BABA", "baba": "BABA", "jd.com": "JD", "pinduoduo": "PDD",
    "tencent": "TCEHY", "baidu": "BIDU", "nio": "NIO", "xpeng": "XPEV",
    "spy": "SPY", "qqq": "QQQ", "iwm": "IWM", "dia": "DIA", "vti": "VTI",
    "voo": "VOO", "vug": "VUG", "arkk": "ARKK",
    "soxl": "SOXL", "soxs": "SOXS", "tqqq": "TQQQ", "sqqq": "SQQQ",
    "uvxy": "UVXY", "vxx": "VXX", "svxy": "SVXY",
    "palantir": "PLTR", "pltr": "PLTR", "snowflake": "SNOW", "crowdstrike": "CRWD",
    "datadog": "DDOG", "mongodb": "MDB", "zillow": "Z", "reddit": "RDDT",
    "robinhood": "HOOD", "hood": "HOOD", "sofi": "SOFI", "upstart": "UPST",
    "affirm": "AFRM", "aft": "AFRM", "coinbase": "COIN", "square": "SQ", "block": "SQ",
    "paypal": "PYPL", "visa": "V", "mastercard": "MA",
    "delta": "DAL", "united": "UAL", "american airlines": "AAL", "southwest": "LUV",
    "boeing": "BA", "airbus": "EADSY",
    "exxon": "XOM", "chevron": "CVX", "conocophillips": "COP", "shell": "SHEL",
    "bp": "BP", "occidental": "OXY", "oxy": "OXY",
    "coca cola": "KO", "pepsi": "PEP", "mcdonalds": "MCD", "starbucks": "SBUX",
    "nike": "NKE", "disney": "DIS", "netflix": "NFLX",
    "caterpillar": "CAT", "deere": "DE", "honeywell": "HON", "3m": "MMM",
    "ge": "GE", "general electric": "GE", "raytheon": "RTX", "lockheed": "LMT",
}

RISK_KEYWORDS = {
    "geopolitical": ["war", "conflict", "tariff", "trade war", "sanction", "tensions", "taiwan", "china", "embargo"],
    "supply_chain": ["shortage", "logistics", "shipping", "factory closure", "semiconductor shortage", "bottleneck", "delay"],
    "macro_economics": ["inflation", "fed rate", "cpi", "pce", "recession", "interest rate"],
    "fundamental": ["dcf", "intrinsic value", "moat", "roic", "fcf", "margin of safety", "damodaran"],
    "tech_hardware": ["arm", "x86", "risc-v", "overheating", "yield", "process node", "tape-out"],
    "tech_ai": ["vram", "quantization", "llama.cpp", "vllm", "inference", "batch size"]
}

# Fintech-specific constants
FINTECH_SOURCES = ["stocktwits", "apewisdom", "reddit"]

FINTECH_SOURCE_WEIGHTS = {
    "stocktwits": 0.9,
    "apewisdom": 0.85,
    "reddit": 0.6
}

FINTECH_VALIDATION_KEYWORDS = {
    "stocktwits": {"bullish", "bearish", "long", "short", "calls", "puts", "moon", "tendies", "dip", "rip"},
    "apewisdom": {"wsb", "wallstreetbets", "yolo", "diamond hands", "paper hands", "tendies", "gme", "amc"},
    "reddit": {"dd", "yolo", "hodl", "rocket", "diamond", "paper", "tendies"}
}

FINTECH_TICKER_PATTERNS = {
    "stocktwits": r"\$([A-Z]{1,5})\b",
    "apewisdom": r"\b([A-Z]{1,5})\b",
    "reddit": r"\b([A-Z]{1,5})\b"
}

__all__ = [
    "TICKER_BLACKLIST",
    "VALIDATION_KEYWORDS",
    "FINANCIAL_LEXICON",
    "ENTITY_RESOLUTION",
    "RISK_KEYWORDS",
    "FINTECH_SOURCES",
    "FINTECH_SOURCE_WEIGHTS",
    "FINTECH_VALIDATION_KEYWORDS",
    "FINTECH_TICKER_PATTERNS"
]