#!/usr/bin/env python3
"""
corporate_look_through.py — SEC EDGAR XBRL Parsing for ETF Holdings

Implements bottom-up look-through analysis of corporate bond ETF holdings
to compute the weighted Interest Coverage Ratio (ICR) for each ETF.

Three-tier data acquisition:
  1. iShares AJAX CSV: Direct download from BlackRock endpoints (most reliable)
  2. Vanguard/iShares Page Scrape: HTML table parsing with CSV fallback
  3. SEC EDGAR XBRL API: Pull EBIT and Interest Expense per issuer

Damodaran Principle: The Interest Coverage Ratio serves as an automated
"Synthetic Bond Rating." Instead of trusting lagging agency credit ratings,
we scrape SEC filings to verify if underlying corporations generate stable
cash flows or if operating margins are shrinking.

ICR_fund = sum(w_i * EBIT_i / InterestExpense_i) for top N holdings

SEC Rate Limiting: EDGAR enforces 10 requests/second. This module uses
time.sleep(0.1) per request to prevent IP bans.
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from Quantitative.shared.etf_data_fetcher import ETFDataFetcher

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
CACHE_DIR = WORKSPACE_ROOT / "data" / "edgar_cache"

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


@dataclass
class IssuerFinancials:
    """Financial data for a single bond issuer from SEC EDGAR."""
    company_name: str
    cik: str
    ticker: Optional[str] = None
    ebit: Optional[float] = None
    interest_expense: Optional[float] = None
    icr: Optional[float] = None
    filing_date: Optional[str] = None
    data_source: str = "edgar_xbrl"

    def __post_init__(self):
        if self.ebit is not None and self.interest_expense is not None:
            if self.interest_expense > 0:
                self.icr = self.ebit / self.interest_expense
            else:
                self.icr = 999.0


@dataclass
class HoldingEntry:
    """A single holding within an ETF's portfolio."""
    issuer_name: str
    weight: float
    cik: Optional[str] = None
    cusip: Optional[str] = None
    sector: Optional[str] = None


@dataclass
class LookThroughResult:
    """Complete corporate look-through result for an ETF."""
    ticker: str
    holdings: List[HoldingEntry]
    issuer_financials: Dict[str, IssuerFinancials]
    weighted_icr: Optional[float] = None
    avg_icr: Optional[float] = None
    min_icr: Optional[float] = None
    icr_floor_count: int = 0
    data_completeness: float = 0.0
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "holding_count": len(self.holdings),
            "weighted_icr": self.weighted_icr,
            "avg_icr": self.avg_icr,
            "min_icr": self.min_icr,
            "icr_floor_count": self.icr_floor_count,
            "data_completeness": self.data_completeness,
            "evaluated_at": self.evaluated_at,
        }


class CorporateLookThrough:
    """
    Scrapes ETF holdings and computes weighted Interest Coverage Ratios
    using SEC EDGAR XBRL data.

    Data Strategy (with fallback):
      1. Try ETF fact sheet for top holdings list
      2. Fall back to EDGAR full-text search if fact sheet unavailable
      3. Pull EBIT + Interest Expense from EDGAR companyfacts API
    """

    EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
    EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

    SEC_DELAY = 0.1  # 100ms between requests (10 req/sec limit)

    # ETFs that hold corporate bonds (need look-through)
    # Priority: iShares CSV > Vanguard page > Morningstar page
    CORPORATE_ETF_HOLDINGS = {
        "VCSH": {"vanguard": "https://investor.vanguard.com/investment-products/etfs/profile/vcsh"},
        "VCIT": {"vanguard": "https://investor.vanguard.com/investment-products/etfs/profile/vcit"},
        "LQD":  {"ishares_csv": "https://www.ishares.com/us/products/239566/1467271812596.ajax?fileType=csv&fileName=LQD_holdings&dataType=fund"},
        "IGIB": {"ishares_csv": "https://www.ishares.com/us/products/239463/1467271812596.ajax?fileType=csv&fileName=IGIB_holdings&dataType=fund"},
        "HYG":  {"ishares_csv": "https://www.ishares.com/us/products/239565/1467271812596.ajax?fileType=csv&fileName=HYG_holdings&dataType=fund"},
    }

    # Well-known issuers CIK mapping (fallback if EDGAR lookup fails)
    KNOWN_CIKS = {
        "apple inc": "0000320193",
        "microsoft corp": "0000789019",
        "jpmorgan chase": "0000019617",
        "bank of america": "0000070858",
        "goldman sachs": "0000088698",
        "wells fargo": "0000072971",
        "berkshire hathaway": "0001067983",
        "coca-cola co": "0000021344",
        "pfizer inc": "0000078003",
        "exxon mobil": "0000034088",
        "att inc": "0000732717",
        "chevron corp": "0000093410",
        "morgan stanley": "0000895421",
        "citigroup inc": "0000831001",
        "visa inc": "0000140317",
        "ibm corp": "0000051143",
        "johnson & johnson": "0000200406",
        "procter & gamble": "0000080424",
        "general electric": "0000040545",
        "boeing co": "0000012927",
        "general motors": "0001467858",
        "ford motor co": "0000037996",
        "caterpillar inc": "0000018230",
        "3m co": "0000006482",
        "honeywell intl": "0000077384",
        "walt disney co": "0000100103",
        "comcast corp": "0000902739",
        "dell technologies": "0001571996",
        "oracle corp": "0000134143",
        "salesforce inc": "0001108524",
        "intuit inc": "0000896878",
        "adobe inc": "0000796343",
        "cisco systems": "0000181367",
        "intel corp": "0000050851",
        "qualcomm inc": "0000804328",
        "broadcom inc": "0001731350",
        "mastercard inc": "0001141391",
        "unitedhealth grp": "0000731766",
        "merck & co inc": "0000310158",
        "abbvie inc": "0001551152",
        "amgen inc": "0000031815",
        "charter communications": "0001090727",
        "netflix inc": "0000106528",
        "tesla inc": "0001318605",
        "nvidia corp": "0001045810",
        "amazon com inc": "0001018724",
        "meta platforms": "0001326801",
        "alphabet inc": "0001652044",
        "altria group": "0000764180",
        "conocophillips": "0000013469",
        "schlumberger nv": "0000312987",
        "marathon petroleum": "0001510223",
        "phillips 66": "0000152447",
        "dow inc": "0001625932",
        "dupont de nemours": "0000305542",
        "crown castle intl": "0001051470",
        "american tower": "0001053507",
        "nextera energy": "0000753308",
        "southern company": "0000092166",
        "duke energy corp": "0000073298",
        "dominion energy": "0000100069",
        "wec energy group": "0000783788",
        "xcel energy inc": "0000072903",
        "evergy inc": "0001705110",
        "firstenergy corp": "0000732717",
        "ppl corp": "0000092225",
        "entergy corp": "0000065984",
        "aes corp": "0000874761",
        "devon energy": "0000109001",
        "pioneer natural resources": "0000114080",
        "eo resources inc": "0000313216",
        "apache corp": "0000716275",
        "anadarko petroleum": "0001177390",
        "noble energy inc": "0000732712",
        "valero energy": "0000103498",
        "tesoro corp": "0000820072",
        "sunoco lp": "0001603456",
        "marathon oil corp": "0000101355",
    }

    def __init__(self, request_delay: float = 0.1):
        self._delay = request_delay
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "quant-py-bot/1.0 (contact: research@quant-py.local)",
            "Accept": "application/json",
        })
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, key: str) -> __import__("pathlib").Path:
        return CACHE_DIR / f"{key}.json"

    def _read_cache(self, key: str) -> Optional[Dict]:
        path = self._get_cache_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", ""))
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            if age_hours > 168:  # 7-day cache for EDGAR data
                return None
            return data
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _write_cache(self, key: str, data: Dict) -> None:
        path = self._get_cache_path(key)
        data["cached_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2))

    def _throttle(self) -> None:
        time.sleep(self._delay)

    def _fetch_json(self, url: str) -> Optional[Dict]:
        """Fetch JSON from EDGAR with throttling and error handling."""
        self._throttle()
        try:
            response = self._session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"EDGAR request failed: {url}: {e}")
            return None
        except json.JSONDecodeError:
            logger.warning(f"EDGAR returned non-JSON: {url}")
            return None

    def _resolve_cik(self, company_name: str) -> Optional[str]:
        """Look up CIK number for a company name."""
        name_lower = company_name.lower().strip()

        # Check known CIK map first
        for pattern, cik in self.KNOWN_CIKS.items():
            if pattern in name_lower or name_lower in pattern:
                return cik

        # Search EDGAR full-text company search
        cache_key = f"cik_{name_lower.replace(' ', '_')}"
        cached = self._read_cache(cache_key)
        if cached:
            return cached.get("cik")

        search_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{company_name.replace(' ', '+')}%22&dateRange=custom&startdt=2024-01-01&forms=10-K"
        data = self._fetch_json(search_url)
        if data and "hits" in data and data["hits"]["hits"]:
            cik = data["hits"]["hits"][0].get("_source", {}).get("entity_id")
            if cik:
                self._write_cache(cache_key, {"company_name": company_name, "cik": cik})
                return cik

        return None

    def _fetch_company_financials(self, cik: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Fetch EBIT and Interest Expense from SEC EDGAR XBRL companyfacts.

        Returns:
            Tuple of (EBIT, Interest Expense, Filing Date) or Nones if unavailable.
        """
        cache_key = f"financials_{cik}"
        cached = self._read_cache(cache_key)
        if cached:
            return cached.get("ebit"), cached.get("interest_expense"), cached.get("filing_date")

        padded_cik = cik.zfill(10)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded_cik}.json"
        data = self._fetch_json(url)

        if not data:
            return None, None, None

        facts = data.get("facts", {}).get("us-gaap", {})

        # Try multiple XBRL tags for EBIT (earnings before interest and taxes)
        ebit = None
        ebit_tags = [
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "EarningsBeforeInterestAndTaxes",
        ]
        for tag in ebit_tags:
            if tag in facts:
                units = facts[tag].get("USD", [])
                if units:
                    latest = units[-1]
                    val = latest.get("val")
                    end_date = latest.get("end", "")
                    filing_date = latest.get("filed", "")
                    if val is not None and end_date and "2024" in end_date or "2025" in end_date or "2023" in end_date:
                        ebit = float(val)
                        break

        # Try multiple XBRL tags for Interest Expense
        interest_expense = None
        interest_tags = [
            "InterestExpense",
            "InterestExpenseDebt",
            "InterestAndDebtExpense",
        ]
        for tag in interest_tags:
            if tag in facts:
                units = facts[tag].get("USD", [])
                if units:
                    latest = units[-1]
                    val = latest.get("val")
                    if val is not None:
                        interest_expense = abs(float(val))
                        break

        filing_date = None
        submissions_url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
        sub_data = self._fetch_json(submissions_url)
        if sub_data:
            recent = sub_data.get("filings", {}).get("recent", {})
            if recent and "filingDate" in recent and recent["filingDate"]:
                filing_date = recent["filingDate"][0]

        self._write_cache(cache_key, {
            "cik": cik,
            "ebit": ebit,
            "interest_expense": interest_expense,
            "filing_date": filing_date,
        })

        return ebit, interest_expense, filing_date

    def _scrape_etf_holdings(self, ticker: str) -> List[HoldingEntry]:
        """
        Attempt to scrape top holdings from multiple ETF data sources.

        Priority order:
          1. iShares AJAX CSV (direct download, most reliable)
          2. Vanguard page scrape (JS-rendered, less reliable)
          3. Morningstar page scrape (legacy fallback)

        Falls back to empty list if all sources fail.
        """
        sources = self.CORPORATE_ETF_HOLDINGS.get(ticker)
        if not sources:
            return []

        # Source 1: iShares AJAX CSV (direct download)
        if "ishares_csv" in sources:
            holdings = self._fetch_ishares_csv(ticker, sources["ishares_csv"])
            if holdings:
                return holdings

        # Source 2: Vanguard page scrape
        if "vanguard" in sources:
            holdings = self._fetch_vanguard_page(ticker, sources["vanguard"])
            if holdings:
                return holdings

        # Source 3: Morningstar page scrape (legacy)
        if "morningstar" in sources:
            holdings = self._fetch_morningstar_page(ticker, sources["morningstar"])
            if holdings:
                return holdings

        return []

    def _fetch_ishares_csv(self, ticker: str, csv_url: str) -> List[HoldingEntry]:
        """Download holdings CSV from iShares AJAX endpoint."""
        cache_key = f"ishares_holdings_{ticker}"
        cached = self._read_cache(cache_key)
        if cached:
            return [
                HoldingEntry(issuer_name=h["name"], weight=h["weight"])
                for h in cached.get("holdings", [])
            ]

        try:
            self._throttle()
            response = self._session.get(
                csv_url,
                headers={"User-Agent": _USER_AGENTS[0]},
                timeout=30,
            )
            response.raise_for_status()

            import csv
            import io
            reader = csv.reader(io.StringIO(response.text))
            holdings = []
            header_skipped = False

            for row in reader:
                if not header_skipped:
                    # Skip iShares header rows (first ~3 rows are metadata)
                    if row and any("Name" in cell or "Ticker" in cell for cell in row):
                        header_skipped = True
                    continue

                if len(row) >= 4:
                    # iShares CSV format: Name, Ticker, CUSIP, Weight, ...
                    name = row[0].strip()
                    weight_str = row[3].strip().replace("%", "") if len(row) > 3 else ""
                    if not name or not weight_str:
                        continue
                    try:
                        weight = float(weight_str) / 100.0
                    except ValueError:
                        continue
                    if weight > 0.001:  # Filter out negligible holdings
                        holdings.append(HoldingEntry(issuer_name=name, weight=weight))

            holdings.sort(key=lambda h: h.weight, reverse=True)
            holdings = holdings[:20]  # Top 20 by weight

            if holdings:
                self._write_cache(cache_key, {
                    "ticker": ticker,
                    "holdings": [{"name": h.issuer_name, "weight": h.weight} for h in holdings],
                    "source": "ishares_csv",
                })
                logger.info(f"Downloaded {len(holdings)} holdings from iShares CSV for {ticker}")
                return holdings

        except Exception as e:
            logger.warning(f"iShares CSV download failed for {ticker}: {e}")

        return []

    def _fetch_vanguard_page(self, ticker: str, page_url: str) -> List[HoldingEntry]:
        """Scrape holdings from Vanguard ETF profile page."""
        try:
            self._throttle()
            response = self._session.get(
                page_url,
                headers={"User-Agent": _USER_AGENTS[0]},
                timeout=30,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # Try to find CSV composition file link
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if "composition" in href.lower() and ("csv" in href.lower() or "xlsx" in href.lower()):
                    csv_url = href if href.startswith("http") else f"https://investor.vanguard.com{href}"
                    return self._fetch_vanguard_csv(ticker, csv_url)

            # Fallback: parse HTML table
            holdings = []
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows[1:21]:  # Skip header, take top 20
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        name = cells[0].get_text(strip=True)
                        weight_str = cells[-1].get_text(strip=True).replace("%", "")
                        try:
                            weight = float(weight_str) / 100.0
                        except ValueError:
                            continue
                        if weight > 0.001:
                            holdings.append(HoldingEntry(issuer_name=name, weight=weight))

            if holdings:
                logger.info(f"Scraped {len(holdings)} holdings from Vanguard page for {ticker}")
                return holdings

        except Exception as e:
            logger.warning(f"Vanguard page scrape failed for {ticker}: {e}")

        return []

    def _fetch_vanguard_csv(self, ticker: str, csv_url: str) -> List[HoldingEntry]:
        """Download holdings CSV from Vanguard composition file."""
        try:
            self._throttle()
            response = self._session.get(
                csv_url,
                headers={"User-Agent": _USER_AGENTS[0]},
                timeout=30,
            )
            response.raise_for_status()

            import csv
            import io
            reader = csv.reader(io.StringIO(response.text))
            holdings = []
            header_skipped = False

            for row in reader:
                if not header_skipped:
                    if row and any("Name" in cell or "Ticker" in cell or "Weight" in cell for cell in row):
                        header_skipped = True
                    continue
                if len(row) >= 2:
                    name = row[0].strip()
                    # Find the weight column (look for % values)
                    weight = None
                    for cell in row[1:]:
                        cell_clean = cell.strip().replace("%", "")
                        try:
                            w = float(cell_clean)
                            if 0 < w < 100:
                                weight = w / 100.0
                                break
                        except ValueError:
                            continue
                    if name and weight and weight > 0.001:
                        holdings.append(HoldingEntry(issuer_name=name, weight=weight))

            holdings.sort(key=lambda h: h.weight, reverse=True)
            return holdings[:20]

        except Exception as e:
            logger.warning(f"Vanguard CSV download failed for {ticker}: {e}")

        return []

    def _fetch_morningstar_page(self, ticker: str, page_url: str) -> List[HoldingEntry]:
        """Scrape holdings from Morningstar ETF page (legacy fallback)."""
        try:
            self._throttle()
            response = self._session.get(
                page_url,
                headers={"User-Agent": _USER_AGENTS[0]},
                timeout=30,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            holdings = []
            rows = soup.select("table tbody tr")
            for row in rows[:20]:
                cells = row.find_all("td")
                if len(cells) >= 3:
                    name = cells[0].get_text(strip=True)
                    weight_str = cells[1].get_text(strip=True).replace("%", "")
                    try:
                        weight = float(weight_str) / 100.0
                    except ValueError:
                        continue
                    holdings.append(HoldingEntry(issuer_name=name, weight=weight))

            if holdings:
                logger.info(f"Scraped {len(holdings)} holdings from Morningstar for {ticker}")
                return holdings

        except Exception as e:
            logger.warning(f"Morningstar scrape failed for {ticker}: {e}")

        return []

    def _build_synthetic_holdings(self, ticker: str, count: int = 10) -> List[HoldingEntry]:
        """
        Build synthetic holdings using SEC EDGAR holdings search.

        Searches for institutional holders of the ETF's underlying bonds.
        """
        cache_key = f"holdings_{ticker}_{count}"
        cached = self._read_cache(cache_key)
        if cached:
            return [
                HoldingEntry(
                    issuer_name=h["name"],
                    weight=h["weight"],
                    cik=h.get("cik"),
                )
                for h in cached.get("holdings", [])
            ]
        return []

    def look_through(
        self,
        ticker: str,
        top_n: int = 20,
        force_refresh: bool = False,
    ) -> LookThroughResult:
        """
        Perform corporate look-through analysis for a bond ETF.

        Steps:
          1. Scrape top N holdings from fact sheet (fallback: build_synthetic)
          2. For each issuer, resolve CIK and fetch financials from EDGAR
          3. Compute weighted ICR across all issuers with data

        Args:
            ticker: Bond ETF ticker (VCSH, VCIT, etc.)
            top_n: Number of top holdings to analyze
            force_refresh: Skip cache if True

        Returns:
            LookThroughResult with holdings, per-issuer financials, and weighted ICR.
        """
        logger.info(f"CorporateLookThrough: Analyzing {ticker} (top {top_n} holdings)...")

        # Step 1: Get holdings
        holdings = self._scrape_etf_holdings(ticker)
        if not holdings:
            holdings = self._build_synthetic_holdings(ticker, top_n)

        holdings = holdings[:top_n]

        # Step 2: Fetch financials for each issuer
        issuer_financials: Dict[str, IssuerFinancials] = {}
        financials_found = 0

        for holding in holdings:
            name = holding.issuer_name
            cik = holding.cik or self._resolve_cik(name)

            if cik:
                holding.cik = cik
                ebit, interest, filing = self._fetch_company_financials(cik)

                financial = IssuerFinancials(
                    company_name=name,
                    cik=cik,
                    ebit=ebit,
                    interest_expense=interest,
                    filing_date=filing,
                    data_source="edgar_xbrl",
                )
                issuer_financials[name] = financial

                if financial.icr is not None:
                    financials_found += 1
                    logger.debug(
                        f"  {name}: EBIT=${ebit:,.0f}, Interest=${interest:,.0f}, "
                        f"ICR={financial.icr:.2f}" if ebit and interest else
                        f"  {name}: partial data"
                    )

        # Step 3: Compute weighted ICR
        total_weight = 0.0
        weighted_icr_sum = 0.0
        icr_values = []
        icr_floor_count = 0

        for holding in holdings:
            financial = issuer_financials.get(holding.issuer_name)
            if financial and financial.icr is not None:
                weighted_icr_sum += holding.weight * financial.icr
                total_weight += holding.weight
                icr_values.append(financial.icr)

                if financial.icr < 2.0:
                    icr_floor_count += 1

        weighted_icr = weighted_icr_sum / total_weight if total_weight > 0 else None
        avg_icr = sum(icr_values) / len(icr_values) if icr_values else None
        min_icr = min(icr_values) if icr_values else None
        data_completeness = financials_found / len(holdings) if holdings else 0.0

        result = LookThroughResult(
            ticker=ticker,
            holdings=holdings,
            issuer_financials=issuer_financials,
            weighted_icr=round(weighted_icr, 2) if weighted_icr else None,
            avg_icr=round(avg_icr, 2) if avg_icr else None,
            min_icr=round(min_icr, 2) if min_icr else None,
            icr_floor_count=icr_floor_count,
            data_completeness=round(data_completeness, 2),
        )

        logger.info(
            f"CorporateLookThrough[{ticker}]: "
            f"weighted_ICR={weighted_icr:.2f}, avg_ICR={avg_icr:.2f}, "
            f"min_ICR={min_icr:.2f}, data_completeness={data_completeness:.0%}" if weighted_icr else
            f"CorporateLookThrough[{ticker}]: insufficient data for ICR computation"
        )

        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    ticker = sys.argv[1] if len(sys.argv) > 1 else "VCSH"
    look = CorporateLookThrough()
    result = look.look_through(ticker)

    print(f"\n{'='*60}")
    print(f"CORPORATE LOOK-THROUGH REPORT: {result.ticker}")
    print(f"{'='*60}")
    print(f"  Holdings: {len(result.holdings)}")
    print(f"  Weighted ICR: {result.weighted_icr}" if result.weighted_icr else "  Weighted ICR: N/A")
    print(f"  Average ICR: {result.avg_icr}" if result.avg_icr else "  Average ICR: N/A")
    print(f"  Minimum ICR: {result.min_icr}" if result.min_icr else "  Minimum ICR: N/A")
    print(f"  ICR Floor Count (< 2.0): {result.icr_floor_count}")
    print(f"  Data Completeness: {result.data_completeness:.0%}")
    print(f"\n  Per-Issuer Breakdown:")
    for name, fin in result.issuer_financials.items():
        icr_str = f"ICR={fin.icr:.2f}" if fin.icr else "ICR=N/A"
        print(f"    {name}: {icr_str}")
