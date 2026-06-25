import os
import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import requests
import yfinance as yf
from bs4 import BeautifulSoup
import re
import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

app = FastAPI(title="SEC & Valuation Data Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Standard Damodaran Industry Betas and EV/Sales Multiples (Stern NYU 2024 / Reference Data)
DAMODARAN_INDUSTRIES = [
    {"industry": "Software (System & Application)", "unlevered_beta": 1.27, "ev_sales": 10.72},
    {"industry": "Computer Services", "unlevered_beta": 0.85, "ev_sales": 7.50},
    {"industry": "Information Services", "unlevered_beta": 0.90, "ev_sales": 5.20},
    {"industry": "Interactive Media", "unlevered_beta": 1.15, "ev_sales": 6.80},
    {"industry": "Retail (Internet)", "unlevered_beta": 1.21, "ev_sales": 2.50},
    {"industry": "Semiconductor", "unlevered_beta": 1.35, "ev_sales": 8.20},
    {"industry": "Semiconductor Equipment", "unlevered_beta": 1.30, "ev_sales": 7.50},
    {"industry": "Computers/Peripherals", "unlevered_beta": 1.05, "ev_sales": 3.80},
    {"industry": "Telecom Services", "unlevered_beta": 0.60, "ev_sales": 1.80},
    {"industry": "Advertising", "unlevered_beta": 0.95, "ev_sales": 2.10},
    {"industry": "Aerospace/Defense", "unlevered_beta": 0.82, "ev_sales": 1.50},
    {"industry": "Air Transport", "unlevered_beta": 0.75, "ev_sales": 0.90},
    {"industry": "Apparel", "unlevered_beta": 0.90, "ev_sales": 1.20},
    {"industry": "Auto & Truck", "unlevered_beta": 0.70, "ev_sales": 0.80},
    {"industry": "Beverage (Soft Drink)", "unlevered_beta": 0.65, "ev_sales": 3.50},
    {"industry": "Chemical (Specialty)", "unlevered_beta": 0.92, "ev_sales": 1.90},
    {"industry": "Drugs (Biotechnology)", "unlevered_beta": 1.35, "ev_sales": 6.50},
    {"industry": "Drugs (Pharmaceutical)", "unlevered_beta": 0.98, "ev_sales": 3.20},
    {"industry": "Electronics (General)", "unlevered_beta": 1.02, "ev_sales": 2.00},
    {"industry": "Financial Services", "unlevered_beta": 0.85, "ev_sales": 3.00},
    {"industry": "Hospitals/Healthcare Providers", "unlevered_beta": 0.68, "ev_sales": 1.10},
    {"industry": "Hotel/Gaming", "unlevered_beta": 0.88, "ev_sales": 2.20},
    {"industry": "Machinery", "unlevered_beta": 0.95, "ev_sales": 1.40},
    {"industry": "Oil/Gas (Integrated)", "unlevered_beta": 0.78, "ev_sales": 1.10},
    {"industry": "Real Estate (General)", "unlevered_beta": 0.55, "ev_sales": 4.50},
    {"industry": "Restaurant/Dining", "unlevered_beta": 0.82, "ev_sales": 1.80},
    {"industry": "Steel", "unlevered_beta": 1.05, "ev_sales": 0.80},
    {"industry": "Utility (General)", "unlevered_beta": 0.45, "ev_sales": 2.20},
]

# Hardcoded fallbacks to guarantee instant answers for major tickers if network or parser fail
TICKER_SEGMENTS_FALLBACK = {
    "MSFT": [
        {"segment": "Software (System & Application)", "revenue": 124008000000},
        {"segment": "Computer Services", "revenue": 87907000000}
    ],
    "ORCL": [
        {"segment": "Computer Services", "revenue": 39000000000},
        {"segment": "Software (System & Application)", "revenue": 11000000000}
    ],
    "AMZN": [
        {"segment": "Retail (Internet)", "revenue": 480000000000},
        {"segment": "Computer Services", "revenue": 90000000000}
    ],
    "GOOGL": [
        {"segment": "Interactive Media", "revenue": 272000000000},
        {"segment": "Computer Services", "revenue": 33000000000}
    ],
    "GOOG": [
        {"segment": "Interactive Media", "revenue": 272000000000},
        {"segment": "Computer Services", "revenue": 33000000000}
    ],
    "CRM": [
        {"segment": "Software (System & Application)", "revenue": 32000000000},
        {"segment": "Information Services", "revenue": 2800000000}
    ],
    "NVDA": [
        {"segment": "Semiconductor", "revenue": 45000000000},
        {"segment": "Computers/Peripherals", "revenue": 15000000000}
    ],
    "AAPL": [
        {"segment": "Computers/Peripherals", "revenue": 298000000000},
        {"segment": "Computer Services", "revenue": 85000000000}
    ],
    "AVGO": [
        {"segment": "Semiconductor", "revenue": 36858000000},
        {"segment": "Software (System & Application)", "revenue": 27029000000}
    ]
}

def get_sec_headers():
    return {"User-Agent": "Valrivn Valk minidragonminidragon@gmail.com"}

def get_company_cik(ticker: str) -> Optional[str]:
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        res = requests.get(url, headers=get_sec_headers(), timeout=5)
        if res.status_code == 200:
            data = res.json()
            for item in data.values():
                if item["ticker"].upper() == ticker.upper():
                    return str(item["cik_str"]).zfill(10)
    except Exception as e:
        print(f"Error resolving CIK for {ticker}: {e}")
    return None

def fetch_segment_table_from_sec(cik: str) -> Optional[dict]:
    """
    Fetches the latest 10-K from SEC EDGAR submissions API, parses the HTML,
    looks for the segment revenue table, and returns both the segments list and the source url.
    """
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        res = requests.get(url, headers=get_sec_headers(), timeout=5)
        if res.status_code != 200:
            return None
        
        recent = res.json()["filings"]["recent"]
        for i, form in enumerate(recent["form"]):
            if form == "10-K":
                acc_num = recent["accessionNumber"][i].replace("-", "")
                doc_name = recent["primaryDocument"][i]
                filing_date = recent["filingDate"][i]
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_num}/{doc_name}"
                
                # Fetch filing HTML
                html_res = requests.get(filing_url, headers=get_sec_headers(), timeout=10)
                if html_res.status_code == 200:
                    soup = BeautifulSoup(html_res.content, "lxml")
                    tables = soup.find_all("table")
                    
                    # Search for tables containing keywords for revenue by segment (excluding operating income)
                    for table in tables:
                        table_text = table.get_text().lower()
                        if "net revenue by segment" in table_text or "revenue by segment" in table_text or "operating segment" in table_text or "revenues by segment" in table_text:
                            # Verify it is not an operating income table
                            if "operating income" in table_text or "operating profit" in table_text:
                                continue
                            
                            # Parse rows
                            extracted = []
                            rows = table.find_all("tr")
                            
                            # Check scaling factor (in millions or in thousands)
                            multiplier = 1.0
                            if "in millions" in table_text or "in millions" in soup.get_text()[:5000].lower():
                                multiplier = 1_000_000.0
                            elif "in thousands" in table_text:
                                multiplier = 1_000.0
                                
                            for row in rows:
                                cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                                if len(cols) >= 2:
                                    name = cols[0].replace("\xa0", " ").strip()
                                    # Regex to match a number possibly containing commas or parentheses
                                    num_match = re.search(r"[-+]?\s*\(?\s*[\d,]+\s*\)?", cols[1])
                                    if num_match:
                                        val_str = num_match.group(0).replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
                                        if val_str.isdigit() and len(name) > 3:
                                            # Skip total or sum indicators
                                            if any(x in name.lower() for x in ["total", "consolidated", "elimination", "corporate"]):
                                                continue
                                            val = float(val_str) * multiplier
                                            extracted.append({"segment": name, "revenue": val})
                                            
                            if len(extracted) > 1:
                                return {
                                    "segments": extracted,
                                    "filing_url": filing_url,
                                    "filing_date": filing_date,
                                    "raw_table_html": str(table)[:2000] # send snippet
                                }
    except Exception as e:
        print(f"Error fetching/parsing SEC data: {e}")
    return None

@app.get("/damodaran-data")
def get_damodaran():
    return DAMODARAN_INDUSTRIES

@app.get("/scrape")
def scrape_ticker(ticker: str = Query(..., description="Stock Ticker symbol")):
    ticker_clean = ticker.upper().strip()
    
    # Defaults
    rf_rate = 0.0425
    mrp = 0.055
    tax_rate = 0.21
    cost_of_debt = 0.05
    country = "United States"
    filing_url = "https://www.sec.gov/edgar/searchedgar/companysearch.html"
    filing_date = "N/A"
    raw_html_snippet = "N/A"
    source = "Preset Fallback"
    
    # 1. Fetch live Macro parameters
    try:
        # TNX yield
        tnx = yf.Ticker("^TNX")
        tnx_price = tnx.info.get("currentPrice") or tnx.info.get("previousClose")
        if tnx_price:
            tnx_val = float(tnx_price)
            if tnx_val > 10.0:
                rf_rate = tnx_val / 1000
            else:
                rf_rate = tnx_val / 100
            
        # Implied MRP from SPY
        spy = yf.Ticker("SPY")
        forward_pe = spy.info.get("forwardPE") or 20.0
        implied_equity_yield = 1.0 / float(forward_pe)
        mrp = max(0.02, implied_equity_yield - rf_rate)
    except Exception as e:
        print(f"Error fetching macro parameters: {e}")
        
    # 2. Fetch specific Ticker Data
    try:
        yf_ticker = yf.Ticker(ticker_clean)
        info = yf_ticker.info
        
        country = info.get("country") or "United States"
        price = info.get("currentPrice") or info.get("previousClose") or 0.0
        shares = info.get("sharesOutstanding") or 1
        market_cap = price * shares if price else info.get("marketCap") or 0.0
        
        # Balance sheet Total Debt & Cash
        bs = yf_ticker.balancesheet
        total_debt = 0.0
        cash = 0.0
        if bs is not None and not bs.empty:
            total_debt = float(bs.loc["Total Debt"].iloc[0]) if "Total Debt" in bs.index else 0.0
            if total_debt == 0.0:
                long_term_debt = float(bs.loc["Long Term Debt"].iloc[0]) if "Long Term Debt" in bs.index else 0.0
                short_term_debt = float(bs.loc["Current Debt"].iloc[0]) if "Current Debt" in bs.index else 0.0
                total_debt = long_term_debt + short_term_debt
            cash = float(bs.loc["Cash And Cash Equivalents"].iloc[0]) if "Cash And Cash Equivalents" in bs.index else 0.0
            
        if total_debt == 0.0:
            total_debt = float(info.get("totalDebt") or 0.0)
        if cash == 0.0:
            cash = float(info.get("totalCash") or 0.0)

        # 3. Calculate company-specific Cost of Debt and Tax Rates
        interest_expense = 0.0
        tax_provision = 0.0
        pretax_income = 0.0
        
        is_df = yf_ticker.income_stmt
        if is_df is not None and not is_df.empty:
            for idx in is_df.index:
                idx_lower = idx.lower()
                if "tax provision" in idx_lower or "tax expense" in idx_lower or "income tax" in idx_lower:
                    tax_provision = float(is_df.loc[idx].iloc[0])
                elif "pretax income" in idx_lower or "income before tax" in idx_lower or "pre-tax" in idx_lower:
                    pretax_income = float(is_df.loc[idx].iloc[0])
                elif "interest expense" in idx_lower:
                    interest_expense = float(is_df.loc[idx].iloc[0])
                    
        # Tax rate calculation
        if tax_provision != 0.0 and pretax_income > 0:
            calc_tax = tax_provision / pretax_income
            if 0.0 <= calc_tax <= 0.6:
                tax_rate = calc_tax
            else:
                if country.lower() in ["us", "usa", "united states"]:
                    tax_rate = 0.21
                else:
                    tax_rate = -1.0 # signal to UI that foreign manual tax rate input is needed
        else:
            if country.lower() not in ["us", "usa", "united states"]:
                tax_rate = -1.0
            else:
                tax_rate = 0.21
                
        # Cost of debt calculation
        if interest_expense != 0.0 and total_debt > 0:
            calc_cod = abs(interest_expense) / total_debt
            if 0.005 <= calc_cod <= 0.25:
                cost_of_debt = calc_cod
            else:
                cost_of_debt = 0.0 # signal to use spread fallback (R_f + 1.5%)
        else:
            cost_of_debt = 0.0

        # 4. Segment Data Extraction
        segments = None
        cik = get_company_cik(ticker_clean)
        if cik:
            sec_res = fetch_segment_table_from_sec(cik)
            if sec_res:
                segments = sec_res["segments"]
                filing_url = sec_res["filing_url"]
                filing_date = sec_res["filing_date"]
                raw_html_snippet = sec_res["raw_table_html"]
                source = "SEC EDGAR Dynamic Scraping"
                
        if not segments:
            # Fallback to local preset dictionary
            segments = TICKER_SEGMENTS_FALLBACK.get(ticker_clean, [
                {"segment": "Software (System & Application)", "revenue": market_cap * 0.1},
                {"segment": "Computer Services", "revenue": market_cap * 0.05}
            ])
            source = f"Fallback Preset for {ticker_clean}" if ticker_clean in TICKER_SEGMENTS_FALLBACK else "Default Dynamic Placeholder"
            
        return {
            "ticker": ticker_clean,
            "current_price": price,
            "shares_outstanding": shares,
            "market_cap": market_cap,
            "total_debt": total_debt,
            "cash": cash,
            "rf_rate": rf_rate,
            "mrp": mrp,
            "tax_rate": tax_rate,
            "cost_of_debt": cost_of_debt,
            "country": country,
            "segments": segments,
            "filing_url": filing_url,
            "filing_date": filing_date,
            "raw_html_snippet": raw_html_snippet,
            "source": source
        }
    except Exception as e:
        return {
            "error": str(e),
            "ticker": ticker_clean,
            "current_price": 0.0,
            "shares_outstanding": 1,
            "market_cap": 0.0,
            "total_debt": 0.0,
            "cash": 0.0,
            "rf_rate": rf_rate,
            "mrp": mrp,
            "tax_rate": tax_rate,
            "cost_of_debt": cost_of_debt,
            "country": country,
            "segments": [
                {"segment": "Software (System & Application)", "revenue": 100000000},
                {"segment": "Computer Services", "revenue": 50000000}
            ],
            "filing_url": filing_url,
            "filing_date": filing_date,
            "raw_html_snippet": raw_html_snippet,
            "source": "Error Fallback"
        }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
