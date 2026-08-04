"""Equity-sleeve universe roster.

50 names in three groups:
  A — megacap bias names (10, bias=True)
  B — tech peers (30, bias=False)
  C — same-beta non-tech controls (10, bias=False)
"""

GROUP_A_MEGACAPS = [
    "NVDA", "AMD", "INTC", "AVGO", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN",
]

GROUP_B_TECH = [
    "QCOM", "MRVL", "MU", "SWKS", "LSCC", "TSM", "CRM", "ADBE", "NOW", "WDAY",
    "DELL", "HPQ", "IBM", "HPE", "SMCI", "ANET", "ORCL", "CSCO", "TXN", "AMAT",
    "LRCX", "KLAC", "ASML", "PANW", "SNOW", "INTU", "ACN", "CTSH", "SAP", "STX",
]

GROUP_C_SAME_BETA = [
    "JPM", "BAC", "XOM", "CVX", "WMT", "PG", "JNJ", "HD", "UNH", "DIS",
]

_SECTOR = {
    "NVDA": "semiconductor", "AMD": "semiconductor", "INTC": "semiconductor",
    "AVGO": "semiconductor", "MSFT": "platform_software", "GOOGL": "cloud_internet",
    "META": "cloud_internet", "TSLA": "consumer_cyclical", "AAPL": "consumer_electronics",
    "AMZN": "consumer_cyclical",
    "QCOM": "semiconductor", "MRVL": "semiconductor", "ARM": "semiconductor",
    "MU": "semiconductor", "SWKS": "semiconductor", "LSCC": "semiconductor",
    "TSM": "semiconductor", "CRM": "platform_software", "ADBE": "platform_software",
    "NOW": "platform_software", "WDAY": "platform_software", "DELL": "hardware_oem",
    "HPQ": "hardware_oem", "IBM": "hardware_oem", "HPE": "hardware_oem",
    "SMCI": "hardware_oem", "ANET": "networking", "ORCL": "platform_software",
    "CSCO": "networking", "TXN": "semiconductor", "AMAT": "semiconductor",
    "LRCX": "semiconductor", "KLAC": "semiconductor", "ASML": "semiconductor",
    "PANW": "networking", "SNOW": "cloud_internet", "INTU": "platform_software",
    "ACN": "platform_software", "CTSH": "platform_software", "SAP": "platform_software",
    "STX": "hardware_oem",
    "JPM": "financials", "BAC": "financials", "XOM": "energy", "CVX": "energy",
    "WMT": "consumer_defensive", "PG": "consumer_defensive", "JNJ": "healthcare",
    "HD": "consumer_cyclical", "UNH": "healthcare", "DIS": "consumer_cyclical",
}

_CIK = {
    "NVDA": "0001045810", "AVGO": "0001730168", "INTC": "0000050863",
    "AMD": "0000002488", "MSFT": "0000789019", "GOOGL": "0001652044",
    "META": "0001326801", "TSLA": "0001318605", "AAPL": "0000320193",
    "AMZN": "0001018724",
}


def _build_universe():
    rows = []
    for ticker in GROUP_A_MEGACAPS:
        rows.append({
            "ticker": ticker,
            "group": "A",
            "bias": True,
            "sector": _SECTOR[ticker],
            "sec_cik": _CIK.get(ticker),
        })
    for ticker in GROUP_B_TECH:
        rows.append({
            "ticker": ticker,
            "group": "B",
            "bias": False,
            "sector": _SECTOR[ticker],
            "sec_cik": _CIK.get(ticker),
        })
    for ticker in GROUP_C_SAME_BETA:
        rows.append({
            "ticker": ticker,
            "group": "C",
            "bias": False,
            "sector": _SECTOR[ticker],
            "sec_cik": _CIK.get(ticker),
        })
    return rows


UNIVERSE = _build_universe()


def get_universe(include_bias: bool = True) -> list:
    """Return all 50 names, or only the 40 non-bias names when include_bias=False."""
    if include_bias:
        return list(UNIVERSE)
    return [row for row in UNIVERSE if not row["bias"]]


def get_group(group: str) -> list:
    """Return the roster rows for a single group letter ("A", "B", or "C")."""
    return [row for row in UNIVERSE if row["group"] == group]


def get_cik(ticker) -> str | None:
    """Return the SEC CIK for a ticker, or None if unknown.

    Checks the hard-coded megacap map first (offline-safe), then the SEC
    ticker->CIK cache maintained by the B-20260803 P1 resolver so the 40/50
    fundamentals gap for non-megacap names is covered without a code change.
    """
    cik = _CIK.get(ticker)
    if cik:
        return cik
    from valuation_alpha.universe.cik_resolver import resolve_cik

    return resolve_cik(ticker)


def bias_names() -> list:
    """Return the tickers of the bias group (Group A)."""
    return list(GROUP_A_MEGACAPS)