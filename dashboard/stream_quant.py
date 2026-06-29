import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import difflib
import warnings
import json
import os
from datetime import datetime, timedelta
import yfinance as yf
from dashboard.tab_sentiment_risk import render_sentiment_risk_tab

try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

st.set_page_config(page_title="Valuation Lab & Beta Sandbox", layout="wide")

# Custom Dark Glassmorphism CSS style
st.markdown("""
    <style>
    .main {
        background-color: #0e1117;
    }
    h1, h2, h3 {
        color: #00ffd0 !important;
        font-family: 'Outfit', sans-serif;
    }
    .stAlert {
        border-radius: 10px;
    }
    .metric-card {
        background: rgba(255, 255, 255, 0.05);
        padding: 20px;
        border-radius: 10px;
        border: 1px solid rgba(255, 255, 255, 0.1);
        text-align: center;
    }
    .metric-label {
        font-size: 0.9em;
        color: #8892b0;
    }
    .metric-value {
        font-size: 1.8em;
        font-weight: bold;
        color: #00ffd0;
    }
    </style>
    """, unsafe_allow_html=True)

# ----------------- LOCAL DATA & CONFIG -----------------
DAMODARAN_DATA = [
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

SECTOR_ETF_CANDIDATES = {
    "Semiconductor": ["SMH", "SOXX"],
    "Software (System & Application)": ["IGV", "XSW"],
    "Computers/Peripherals": ["XLK", "IYW"],
    "Computer Services": ["XLK", "IYW"],
    "Interactive Media": ["XLC", "FCOM"],
    "Information Services": ["XLC", "IYW"],
    "Retail (Internet)": ["XLY", "IBUY"],
    "Retail (General)": ["XLY", "XRT"],
    "Financial Services": ["XLF", "IYF"],
    "Oil/Gas (Integrated)": ["XLE", "VDE"],
    "Utility (General)": ["XLU", "IDU"],
    "Aerospace/Defense": ["ITA", "PPA"]
}

# ----------------- CACHED ETF MATCHING ENGINE -----------------
CACHE_FILE = "etf_selection_cache.json"

def get_optimal_etf(sector: str) -> tuple:
    """
    Selects the optimal ETF based on trading volume (70%) and total assets (30%).
    Caches the selection for 7 days (weekly measure).
    """
    now = datetime.now()
    cache_data = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cache_data = json.load(f)
        except Exception:
            pass

    if sector in cache_data:
        entry = cache_data[sector]
        cache_time = datetime.fromisoformat(entry["timestamp"])
        if now - cache_time < timedelta(days=7):
            return entry["etf"], entry["timestamp"]

    candidates = SECTOR_ETF_CANDIDATES.get(sector, ["XLK"])
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        best_score = -1.0
        chosen = candidates[0]
        
        # Volumes and assets collection
        volumes = []
        assets = []
        for ticker in candidates:
            try:
                t_obj = yf.Ticker(ticker)
                info = t_obj.info
                vol = float(info.get("averageVolume") or info.get("volume") or 1.0)
                asset = float(info.get("totalAssets") or 1.0)
                volumes.append(vol)
                assets.append(asset)
            except Exception:
                volumes.append(1.0)
                assets.append(1.0)

        # Min-max scale and score
        max_vol = max(volumes) if volumes else 1.0
        max_asset = max(assets) if assets else 1.0
        
        for i, ticker in enumerate(candidates):
            norm_vol = volumes[i] / max_vol
            norm_asset = assets[i] / max_asset
            score = 0.7 * norm_vol + 0.3 * norm_asset
            if score > best_score:
                best_score = score
                chosen = ticker

    # Save to cache
    timestamp_str = now.isoformat()
    cache_data[sector] = {"etf": chosen, "timestamp": timestamp_str}
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f)
    except Exception:
        pass

    return chosen, timestamp_str

# Fuzzy finder algorithm returning match and confidence score
def match_industry_confidence(name: str, industry_list: list) -> tuple:
    name_clean = name.lower().strip()
    names = [ind["industry"] for ind in industry_list]
    
    matches = difflib.get_close_matches(name_clean, [n.lower() for n in names], n=1, cutoff=0.1)
    if matches:
        matched_lower = matches[0]
        for ind in industry_list:
            if ind["industry"].lower() == matched_lower:
                confidence = difflib.SequenceMatcher(None, name_clean, matched_lower).ratio()
                return ind, confidence
                
    best_score = 0.0
    best_match = industry_list[0]
    for ind in industry_list:
        ind_clean = ind["industry"].lower()
        words_seg = set(name_clean.split())
        words_ind = set(ind_clean.split())
        intersect = words_seg.intersection(words_ind)
        if intersect:
            score = len(intersect) / max(len(words_seg), len(words_ind))
        else:
            score = 0.0
        if score > best_score:
            best_score = score
            best_match = ind
            
    return best_match, best_score

# ----------------- SESSION STATE OVERRIDES -----------------
if "sector_overrides" not in st.session_state:
    st.session_state.sector_overrides = {}

# ----------------- SIDEBAR / CONTROL CENTER -----------------
st.sidebar.title("Configuration & Inputs")
st.sidebar.markdown("### Peer Set Baseline")
st.sidebar.warning("⚠️ **Peer Baseline accuracy warning:** To provide a close bottom-up baseline, you need to compare with **4 to 5 other similar stocks**.")

# Stock Tickers inputs
st.sidebar.subheader("Selected Tickers")
primary_ticker = st.sidebar.text_input("Primary Stock Ticker (e.g. MSFT)", "MSFT").upper().strip()

# ----------------- FETCH BACKEND DATA FOR PRIMARY -----------------
backend_url = "http://127.0.0.1:8000"
primary_scraped = None

try:
    response = requests.get(f"{backend_url}/scrape?ticker={primary_ticker}", timeout=5)
    if response.status_code == 200:
        res_json = response.json()
        if "error" not in res_json:
            primary_scraped = res_json
except Exception:
    pass

if not primary_scraped:
    # Local fallback presets
    primary_scraped = {
        "ticker": primary_ticker,
        "current_price": 400.0,
        "shares_outstanding": 7400000000 if primary_ticker == "MSFT" else 4700000000,
        "market_cap": 2960000000000 if primary_ticker == "MSFT" else 1880000000000,
        "total_debt": 100000000000 if primary_ticker == "MSFT" else 65000000000,
        "cash": 80000000000 if primary_ticker == "MSFT" else 16000000000,
        "rf_rate": 0.0425,
        "mrp": 0.055,
        "tax_rate": 0.21,
        "cost_of_debt": 0.05,
        "country": "United States",
        "segments": [
            {"segment": "Software (System & Application)", "revenue": 124008000000},
            {"segment": "Computer Services", "revenue": 87907000000}
        ] if primary_ticker == "MSFT" else [
            {"segment": "Semiconductor", "revenue": 36858000000},
            {"segment": "Software (System & Application)", "revenue": 27029000000}
        ] if primary_ticker == "AVGO" else [
            {"segment": "Semiconductor", "revenue": 45000000000},
            {"segment": "Computers/Peripherals", "revenue": 15000000000}
        ] if primary_ticker == "NVDA" else [
            {"segment": "Software (System & Application)", "revenue": 100000000},
            {"segment": "Computer Services", "revenue": 50000000}
        ],
        "filing_url": "https://www.sec.gov/edgar/searchedgar/companysearch.html",
        "filing_date": "N/A",
        "raw_html_snippet": "N/A",
        "source": "Local Preset Data"
    }

# Compute primary stock segments to find largest segment
primary_segments_processed = []
primary_tot_value = 0.0
for idx, seg in enumerate(primary_scraped["segments"]):
    seg_name = seg["segment"]
    unique_key = f"{primary_ticker}_{seg_name}_{idx}"
    
    if unique_key in st.session_state.sector_overrides:
        override_ind_name = st.session_state.sector_overrides[unique_key]
        matched = next(ind for ind in DAMODARAN_DATA if ind["industry"] == override_ind_name)
    else:
        matched, _ = match_industry_confidence(seg_name, DAMODARAN_DATA)
        
    est_val = seg["revenue"] * matched["ev_sales"]
    primary_tot_value += est_val
    primary_segments_processed.append({
        "name": seg_name,
        "revenue": seg["revenue"],
        "industry": matched["industry"],
        "ev_sales": matched["ev_sales"],
        "unlevered_beta": matched["unlevered_beta"],
        "estimated_value": est_val
    })

for seg in primary_segments_processed:
    seg["weight"] = seg["estimated_value"] / primary_tot_value if primary_tot_value > 0 else 0.0

# Largest segment determines the ETF mapping
largest_segment = max(primary_segments_processed, key=lambda x: x["weight"]) if primary_segments_processed else {"industry": "Software (System & Application)"}
largest_industry = largest_segment["industry"]

# Find optimal ETF based on trading volume and assets
optimal_etf_ticker, cache_timestamp_str = get_optimal_etf(largest_industry)

# ----------------- SCAN ETF HOLDINGS FOR PEERS -----------------
suggested_peers = []
scan_error = None

try:
    etf_obj = yf.Ticker(optimal_etf_ticker)
    holdings_df = etf_obj.funds_data.top_holdings
    if holdings_df is not None and not holdings_df.empty:
        # Get up to 10 holdings
        candidate_holdings = [str(idx).strip() for idx in holdings_df.index if str(idx).strip() != primary_ticker][:10]
        
        # Calculate distances
        for cand in candidate_holdings:
            # Query backend/preset for cand segments
            cand_scraped = None
            try:
                c_res = requests.get(f"{backend_url}/scrape?ticker={cand}", timeout=3)
                if c_res.status_code == 200:
                    cand_scraped = c_res.json()
            except Exception:
                pass
            
            if not cand_scraped or "error" in cand_scraped:
                # Load fallback presets
                presets = {
                    "MSFT": [{"segment": "Software (System & Application)", "revenue": 0.6}, {"segment": "Computer Services", "revenue": 0.4}],
                    "ORCL": [{"segment": "Computer Services", "revenue": 0.75}, {"segment": "Software (System & Application)", "revenue": 0.25}],
                    "AMZN": [{"segment": "Retail (Internet)", "revenue": 0.8}, {"segment": "Computer Services", "revenue": 0.2}],
                    "NVDA": [{"segment": "Semiconductor", "revenue": 0.75}, {"segment": "Computers/Peripherals", "revenue": 0.25}],
                    "AVGO": [{"segment": "Semiconductor", "revenue": 0.6}, {"segment": "Software (System & Application)", "revenue": 0.4}],
                    "QCOM": [{"segment": "Semiconductor", "revenue": 0.8}, {"segment": "Information Services", "revenue": 0.2}],
                    "TXN": [{"segment": "Semiconductor", "revenue": 0.9}, {"segment": "Electronics (General)", "revenue": 0.1}],
                    "AMD": [{"segment": "Semiconductor", "revenue": 0.95}, {"segment": "Computers/Peripherals", "revenue": 0.05}],
                    "INTC": [{"segment": "Semiconductor", "revenue": 0.85}, {"segment": "Computers/Peripherals", "revenue": 0.15}],
                }
                cand_segs = presets.get(cand, [{"segment": "Semiconductor", "revenue": 1.0}])
            else:
                cand_segs = cand_scraped["segments"]
                
            # Align and vectorise
            primary_vector = {seg["industry"]: seg["weight"] for seg in primary_segments_processed}
            
            # Compute cand segment weights
            cand_processed = []
            cand_tot_val = 0.0
            for cs in cand_segs:
                c_matched, _ = match_industry_confidence(cs["segment"], DAMODARAN_DATA)
                c_val = cs["revenue"] * c_matched["ev_sales"]
                cand_tot_val += c_val
                cand_processed.append({"industry": c_matched["industry"], "value": c_val})
                
            cand_vector = {}
            for cp in cand_processed:
                w = cp["value"] / cand_tot_val if cand_tot_val > 0 else 0.0
                cand_vector[cp["industry"]] = cand_vector.get(cp["industry"], 0.0) + w
                
            # Compute Euclidean Distance
            all_inds = set(list(primary_vector.keys()) + list(cand_vector.keys()))
            dist_sq = sum((primary_vector.get(ind, 0.0) - cand_vector.get(ind, 0.0)) ** 2 for ind in all_inds)
            dist = np.sqrt(dist_sq)
            
            suggested_peers.append({"ticker": cand, "distance": dist})
            
        suggested_peers = sorted(suggested_peers, key=lambda x: x["distance"])
except Exception as e:
    scan_error = str(e)
    # Basic static fallback suggestions if yfinance fund data fails
    suggested_peers = [
        {"ticker": "NVDA", "distance": 0.15},
        {"ticker": "AVGO", "distance": 0.25},
        {"ticker": "QCOM", "distance": 0.35},
        {"ticker": "AMD", "distance": 0.40}
    ]

# Display Optimal ETF details
st.sidebar.markdown(f"**Optimal Proxy Sector ETF:** `{optimal_etf_ticker}`")
st.sidebar.caption(f"Weekly updated: {datetime.fromisoformat(cache_timestamp_str).strftime('%Y-%m-%d %H:%M')}")

# Autofill Button Action
if "peer_inputs" not in st.session_state:
    st.session_state.peer_inputs = ["ORCL", "AMZN", "", ""]

best_4_peers = [s["ticker"] for s in suggested_peers[:4]]
while len(best_4_peers) < 4:
    best_4_peers.append("")

if st.sidebar.button("🤖 Autofill All Recommended Peers"):
    st.session_state.peer_inputs = best_4_peers
    st.rerun()

# Peer Inputs
peer_1 = st.sidebar.text_input("Peer Stock 1", st.session_state.peer_inputs[0], key="p1_input").upper().strip()
if len(suggested_peers) > 0:
    st.sidebar.caption(f"💡 Recommended: **{suggested_peers[0]['ticker']}** (dist: {suggested_peers[0]['distance']:.2f})")

peer_2 = st.sidebar.text_input("Peer Stock 2", st.session_state.peer_inputs[1], key="p2_input").upper().strip()
if len(suggested_peers) > 1:
    st.sidebar.caption(f"💡 Recommended: **{suggested_peers[1]['ticker']}** (dist: {suggested_peers[1]['distance']:.2f})")

peer_3 = st.sidebar.text_input("Peer Stock 3", st.session_state.peer_inputs[2], key="p3_input").upper().strip()
if len(suggested_peers) > 2:
    st.sidebar.caption(f"💡 Recommended: **{suggested_peers[2]['ticker']}** (dist: {suggested_peers[2]['distance']:.2f})")

peer_4 = st.sidebar.text_input("Peer Stock 4", st.session_state.peer_inputs[3], key="p4_input").upper().strip()
if len(suggested_peers) > 3:
    st.sidebar.caption(f"💡 Recommended: **{suggested_peers[3]['ticker']}** (dist: {suggested_peers[3]['distance']:.2f})")

# Update inputs in session state
st.session_state.peer_inputs = [peer_1, peer_2, peer_3, peer_4]
peer_tickers = [t for t in [peer_1, peer_2, peer_3, peer_4] if t]
all_tickers = [primary_ticker] + peer_tickers

# ----------------- VALUATION DYNAMIC PARAMETERS -----------------
# Query backend for all selected tickers
stocks_data = {}
for ticker in all_tickers:
    if ticker == primary_ticker:
        stocks_data[ticker] = primary_scraped
    else:
        c_scraped = None
        try:
            response = requests.get(f"{backend_url}/scrape?ticker={ticker}", timeout=5)
            if response.status_code == 200:
                res_json = response.json()
                if "error" not in res_json:
                    stocks_data[ticker] = res_json
        except Exception:
            pass
        
        if not c_scraped:
            # Fallback segments preset
            presets_full = {
                "ORCL": [
                    {"segment": "Computer Services", "revenue": 39000000000},
                    {"segment": "Software (System & Application)", "revenue": 11000000000}
                ],
                "AMZN": [
                    {"segment": "Retail (Internet)", "revenue": 480000000000},
                    {"segment": "Computer Services", "revenue": 90000000000}
                ],
                "NVDA": [
                    {"segment": "Semiconductor", "revenue": 45000000000},
                    {"segment": "Computers/Peripherals", "revenue": 15000000000}
                ],
                "AVGO": [
                    {"segment": "Semiconductor", "revenue": 36858000000},
                    {"segment": "Software (System & Application)", "revenue": 27029000000}
                ]
            }
            default_segs = presets_full.get(ticker, [
                {"segment": "Software (System & Application)", "revenue": 100000000},
                {"segment": "Computer Services", "revenue": 50000000}
            ])
            stocks_data[ticker] = {
                "ticker": ticker,
                "current_price": 400.0,
                "shares_outstanding": 4700000000 if ticker == "AVGO" else 2500000000,
                "market_cap": 1880000000000 if ticker == "AVGO" else 1000000000000,
                "total_debt": 65000000000 if ticker == "AVGO" else 80000000000,
                "cash": 16000000000 if ticker == "AVGO" else 20000000000,
                "rf_rate": 0.0425,
                "mrp": 0.055,
                "tax_rate": 0.21,
                "cost_of_debt": 0.05,
                "country": "United States",
                "segments": default_segs,
                "filing_url": "https://www.sec.gov/edgar/searchedgar/companysearch.html",
                "filing_date": "N/A",
                "raw_html_snippet": "N/A",
                "source": "Local Fallback Preset"
            }

# Assumptions Section
st.sidebar.subheader("Valuation Assumptions")
prim_raw = stocks_data[primary_ticker]
dynamic_rf = prim_raw.get("rf_rate", 0.0425)
dynamic_mrp = prim_raw.get("mrp", 0.055)
dynamic_tax = prim_raw.get("tax_rate", 0.21)
dynamic_cod = prim_raw.get("cost_of_debt", 0.05)
primary_country = prim_raw.get("country", "United States")

rf_rate = st.sidebar.slider(f"Risk-Free Rate (Live TNX: {dynamic_rf:.3%})", 0.0, 10.0, float(dynamic_rf * 100), step=0.01) / 100
mrp = st.sidebar.slider(f"Market Risk Premium (Implied: {dynamic_mrp:.3%})", 0.0, 15.0, float(dynamic_mrp * 100), step=0.01) / 100

tax_input_val = dynamic_tax
foreign_tax_warning = False
if dynamic_tax < 0:
    foreign_tax_warning = True
    tax_input_val = 0.25
    st.sidebar.warning(f"🌐 **Foreign Stock ({primary_ticker} - {primary_country}):** Input Effective Tax Rate manually:")

tax_rate = st.sidebar.slider(
    f"Corporate Tax Rate ({'US Fixed' if not foreign_tax_warning else 'Foreign'} : {tax_input_val:.1%})",
    0.0, 50.0, float(tax_input_val * 100), step=1.0
) / 100

cod_warning = False
cod_input_val = dynamic_cod
if dynamic_cod == 0.0:
    cod_warning = True
    cod_input_val = rf_rate + 0.015
    
cost_of_debt = st.sidebar.slider(f"Cost of Debt (Live: {cod_input_val:.2%})", 0.0, 20.0, float(cod_input_val * 100), step=0.1) / 100
if cod_warning:
    st.sidebar.info(f"💡 **Cost of Debt Spread Fallback Applied:** Interest/debt calculations missing. Defaulted to spread ($R_f + 1.5\\%$) = {cod_input_val:.2%}")

# ----------------- PROCESS VALUATION LOGIC FOR SELECTED -----------------
for t, data in stocks_data.items():
    tot_value = 0.0
    segments_processed = []
    
    for idx, seg in enumerate(data["segments"]):
        seg_name = seg["segment"]
        unique_key = f"{t}_{seg_name}_{idx}"
        
        if unique_key in st.session_state.sector_overrides:
            override_ind_name = st.session_state.sector_overrides[unique_key]
            matched = next(ind for ind in DAMODARAN_DATA if ind["industry"] == override_ind_name)
        else:
            matched, confidence = match_industry_confidence(seg_name, DAMODARAN_DATA)
            
        est_val = seg["revenue"] * matched["ev_sales"]
        tot_value += est_val
        segments_processed.append({
            "key": unique_key,
            "name": seg_name,
            "revenue": seg["revenue"],
            "industry": matched["industry"],
            "ev_sales": matched["ev_sales"],
            "unlevered_beta": matched["unlevered_beta"],
            "estimated_value": est_val,
            "confidence": confidence if 'confidence' in locals() else 1.0,
            "auto_matched": matched["industry"]
        })
        
    for seg in segments_processed:
        seg["weight"] = seg["estimated_value"] / tot_value if tot_value > 0 else 0.0
        
    unlevered_beta = sum(seg["weight"] * seg["unlevered_beta"] for seg in segments_processed)
    equity = data["market_cap"]
    debt = data["total_debt"]
    de_ratio = debt / equity if equity > 0 else 0.0
    
    levered_beta = unlevered_beta * (1 + (1 - tax_rate) * de_ratio)
    cost_of_equity = rf_rate + levered_beta * mrp
    tot_cap = equity + debt
    wacc = (equity / tot_cap * cost_of_equity) + (debt / tot_cap * cost_of_debt * (1 - tax_rate)) if tot_cap > 0 else cost_of_equity

    data["unlevered_beta"] = unlevered_beta
    data["levered_beta"] = levered_beta
    data["de_ratio"] = de_ratio
    data["cost_of_equity"] = cost_of_equity
    data["wacc"] = wacc
    data["segments_processed"] = segments_processed
    data["tot_value"] = tot_value

# ----------------- TABS -----------------
tab1, tab2, tab3, tab4 = st.tabs(["📊 Analytical Dashboard", "🎓 Learning Sandbox", "🔍 SEC Audit & Verification", "💬 Social Sentiment & Risk"])


# ----------------- TAB 1: ANALYTICAL DASHBOARD -----------------
with tab1:
    st.title("Valuation & Peer Multi-Industry Analysis")
    
    if len(all_tickers) < 5:
        st.warning(f"⚠️ **Accuracy Baseline Warning:** Currently using {len(all_tickers)} / 5 tickers. Industry averages are less stable with fewer peers.")
    else:
        st.success(f"✅ Baseline satisfies recommended limit with {len(all_tickers)} peer group tickers.")

    prim = stocks_data[primary_ticker]
    st.subheader(f"Key Metrics: {primary_ticker} (Primary Ticker)")
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Unlevered Beta</div><div class='metric-value'>{prim['unlevered_beta']:.2f}</div></div>", unsafe_allow_html=True)
    with m2:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Debt / Equity Ratio</div><div class='metric-value'>{prim['de_ratio']:.2%}</div></div>", unsafe_allow_html=True)
    with m3:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Levered Beta</div><div class='metric-value'>{prim['levered_beta']:.2f}</div></div>", unsafe_allow_html=True)
    with m4:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Cost of Equity</div><div class='metric-value'>{prim['cost_of_equity']:.2%}</div></div>", unsafe_allow_html=True)
    with m5:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>WACC</div><div class='metric-value'>{prim['wacc']:.2%}</div></div>", unsafe_allow_html=True)

    st.markdown("---")
    
    st.subheader("Value-Weight Segment Distribution")
    st.caption("Segments are valued at: Segment Revenue × Industry EV/Sales Multiple. Slices reflect the estimated asset value contribution of each segment.")
    
    cols = st.columns(min(len(all_tickers), 3))
    for idx, ticker in enumerate(all_tickers):
        col_idx = idx % 3
        with cols[col_idx]:
            st.markdown(f"##### {ticker} Segment Composition")
            st_data = stocks_data[ticker]
            
            labels = [s["name"] for s in st_data["segments_processed"]]
            values = [s["estimated_value"] for s in st_data["segments_processed"]]
            
            hover_text = []
            for s in st_data["segments_processed"]:
                rev_pct = (s["revenue"] / sum(x["revenue"] for x in st_data["segments_processed"])) * 100
                txt = (
                    f"Segment: {s['name']}<br>"
                    f"Revenue: ${s['revenue']:,.0f} ({rev_pct:.1f}% of Rev)<br>"
                    f"Mapped Sector: {s['industry']}<br>"
                    f"EV/Sales Multiple: {s['ev_sales']}x<br>"
                    f"Estimated Segment Value: ${s['estimated_value']:,.0f}"
                )
                hover_text.append(txt)

            fig = go.Figure(data=[go.Pie(
                labels=labels,
                values=values,
                textinfo='label+percent',
                hoverinfo='text',
                text=hover_text,
                marker=dict(colors=['#00ffd0', '#0099ff', '#7000ff', '#ff007f', '#ffaa00'])
            )])
            fig.update_layout(
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font=dict(color='#ffffff'),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300
            )
            st.plotly_chart(fig, use_container_width=True, key=f"chart_{ticker}")

    st.markdown("---")

    st.subheader("Industry & Segment Concentration Comparison (5% Margin of Error)")
    st.caption("Verifies if comparison tickers maintain matching segment concentrations relative to the primary stock within 5%.")
    
    comparisons = []
    primary_segs = prim["segments_processed"]
    primary_total_rev = sum(s["revenue"] for s in primary_segs)
    
    for peer_t in peer_tickers:
        peer_data = stocks_data[peer_t]
        peer_segs = peer_data["segments_processed"]
        peer_total_rev = sum(s["revenue"] for s in peer_segs)
        
        for p_seg in primary_segs:
            p_share = p_seg["revenue"] / primary_total_rev
            matched_peer_seg = None
            best_ratio = 0.0
            for ps in peer_segs:
                ratio = difflib.SequenceMatcher(None, p_seg["name"].lower(), ps["name"].lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    matched_peer_seg = ps
            
            if matched_peer_seg and best_ratio > 0.4:
                peer_share = matched_peer_seg["revenue"] / peer_total_rev
                diff = abs(p_share - peer_share)
                comparable = "Yes ✅ (Within 5%)" if diff <= 0.05 else "No ❌ (Diff > 5%)"
                comparisons.append({
                    "Segment (Primary)": p_seg["name"],
                    "Primary Share (%)": f"{p_share:.1%}",
                    "Matched Segment (Peer)": matched_peer_seg["name"],
                    "Peer Ticker": peer_t,
                    "Peer Share (%)": f"{peer_share:.1%}",
                    "Variance (%)": f"{diff:.1%}",
                    "Comparable (5% MoE)": comparable
                })
                
    if comparisons:
        st.dataframe(pd.DataFrame(comparisons), use_container_width=True)
    else:
        st.write("No comparable segments found between primary stock and peers.")

# ----------------- TAB 2: LEARNING SANDBOX -----------------
with tab2:
    st.title("🎓 Interactive Valuation Learning Sandbox")
    
    st.markdown("### 🧬 1. Unlevered Beta & Value Weighting")
    st.write(f"""
    **What it is:**  
    Unlevered Beta ($\beta_U$) represents the operating asset risk of a firm's segments, calculated as the weighted average of the segments' industry unlevered betas.
    
    **Calculated Segment Values and Weights for {primary_ticker}:**
    """)
    
    step_rows = []
    for s in prim["segments_processed"]:
        step_rows.append({
            "Segment Name": s["name"],
            "Revenue": f"${s['revenue']:,.0f}",
            "EV/Sales": f"{s['ev_sales']}x",
            "Est. Segment Value": f"${s['estimated_value']:,.0f}",
            "Weight": f"{s['weight']:.2%}",
            "Ind. Unlevered Beta": f"{s['unlevered_beta']:.2f}",
            "Contribution": f"{s['weight'] * s['unlevered_beta']:.3f}"
        })
    st.table(pd.DataFrame(step_rows))
    st.markdown(f"**Resulting Company Unlevered Beta ($\beta_U$):** `{prim['unlevered_beta']:.3f}`")
    
    st.markdown("### ⚖️ 2. The Hamada Equation")
    st.write("""
    **What it is:**  
    Solves for the **Levered Beta** ($\beta_L$) by adding financial risk (leverage) to the operating Unlevered Beta.
    
    **Formula:**
    $$\\beta_L = \\beta_U \\left[ 1 + (1 - T) \\left( \\frac{D}{E} \\right) \\right]$$
    """)
    st.latex(rf"\beta_L = {prim['unlevered_beta']:.3f}(\beta_U) \times \left[ 1 + (1 - {tax_rate:.2f}(T)) \times \left( \frac{{{prim['total_debt']:,.0f}(D)}}{{{prim['market_cap']:,.0f}(E)}} \right) \right]")
    st.latex(rf"\beta_L = {prim['levered_beta']:.3f}")
    
    st.markdown("### 📈 3. Capital Asset Pricing Model (CAPM)")
    st.write(f"""
    **What it is:**  
    Calculates the required **Cost of Equity** ($R_e$) based on systemic risk.
    
    **Formula:**
    $$R_e = R_f + \\beta_L \\times MRP$$
    
    **Live Inputs used:**
    - **Risk-Free Rate ($R_f$):** Quoted from the 10-Year Treasury Yield (^TNX) = `{rf_rate:.3%}`.
    - **Market Risk Premium (MRP):** Implied from S&P 500 Forward P/E (1/PE of SPY - $R_f$) = `{mrp:.3%}`.
    """)
    st.latex(rf"R_e = {rf_rate:.4f}(R_f) + {prim['levered_beta']:.3f}(\beta_L) \times {mrp:.4f}(MRP)")
    st.latex(rf"R_e = {prim['cost_of_equity']:.4%}")

    st.markdown("### 🕸️ 4. Weighted Average Cost of Capital (WACC)")
    st.write(f"""
    **What it is:**  
    Blends the cost of debt (after tax shield) and cost of equity.
    
    **Formula:**
    $$WACC = \\left( \\frac{{E}}{{V}} \\times R_e \\right) + \\left( \\frac{{D}}{{V}} \\times R_d \\times (1 - T) \\right)$$
    
    **Live Inputs used:**
    - **Cost of Debt ($R_d$):** Trailing Interest Expense / Total Debt = `{cost_of_debt:.2%}`.
    - **Effective Tax Rate ($T$):** Trailing income tax expense / Pretax income = `{tax_rate:.2%}`.
    """)
    e_val = prim["market_cap"]
    d_val = prim["total_debt"]
    v_val = e_val + d_val
    st.latex(rf"WACC = \left( \frac{{{e_val:,.0f}}}{{{v_val:,.0f}}} \times {prim['cost_of_equity']:.4f} \right) + \left( \frac{{{d_val:,.0f}}}{{{v_val:,.0f}}} \times {cost_of_debt:.4f} \times (1 - {tax_rate:.2f}) \right)")
    st.latex(rf"WACC = {prim['wacc']:.4%}")

    st.markdown("---")
    st.markdown("### 📏 5. Peer Finder Heuristic: Euclidean Vector Space")
    st.write(f"""
    **What it is & How it Works:**  
    To find comparable peer companies without scanning all 500+ S&P 500 stocks dynamically, we use an **ETF Proxy Heuristic**:
    1. Identify the dominant segment of the primary stock (for `{primary_ticker}`, this is **{largest_industry}**).
    2. Determine the optimal sector ETF representing that industry (e.g., **{optimal_etf_ticker}**). The selection is optimized weekly using a weighted scoring model:
       $$\\text{{Score}} = 0.70 \\times \\text{{Normalized Average Trading Volume}} + 0.30 \\times \\text{{Normalized Total Assets}}$$
       This prioritizes high liquidity (volume) first and size (assets) second.
    3. Scrape the top holdings of the chosen ETF using `yf.Ticker(etf).funds_data.top_holdings`.
    4. Model the primary stock and the holdings' segment distributions as coordinates (vectors) in a multi-dimensional space.
    5. Calculate the straight-line **Euclidean Distance** between vectors:
       $$d = \\sqrt{{\\sum_{{i}} (W_{{\\text{{primary}}, i}} - W_{{\\text{{holding}}, i}})^2}}$$
       A distance of `0.0` represents identical business segments. The holdings are sorted by distance, recommending the closest 4.
    """)

# ----------------- TAB 3: SEC AUDIT & VERIFICATION -----------------
with tab3:
    st.title("🔍 SEC Audit Trail & Sector Verification")
    st.write("Audits segment mappings, confidence scores, and raw SEC EDGAR data. If mappings are incorrect or fail confidence thresholds, they can be overridden in real-time.")
    
    for ticker in all_tickers:
        data = stocks_data[ticker]
        st.markdown(f"### 🏢 {ticker} ({data.get('country', 'N/A')})")
        st.markdown(f"**Data Source:** `{data.get('source', 'N/A')}` | **Filing Date:** `{data.get('filing_date', 'N/A')}`")
        st.markdown(f"[🔗 View original 10-K document on sec.gov]({data.get('filing_url', '#')})")
        
        # Display Table segments
        seg_rows = []
        low_confidence_flag = False
        
        for idx, s in enumerate(data["segments_processed"]):
            is_low = s["confidence"] < 0.75
            if is_low:
                low_confidence_flag = True
                
            seg_rows.append({
                "Segment Name": s["name"],
                "Scraped Revenue": f"${s['revenue']:,.0f}",
                "Auto-Matched Industry": s["auto_matched"],
                "Confidence Score": f"{s['confidence']:.1%}",
                "Confidence Status": "⚠️ Low (<75%)" if is_low else "✅ Good",
                "Final Mapped Industry": s["industry"]
            })
            
        st.table(pd.DataFrame(seg_rows))
        
        # Interactive Manual Overrides Section
        st.markdown("**Verify / Override Industry Classifications:**")
        cols = st.columns(len(data["segments_processed"]))
        for idx, s in enumerate(data["segments_processed"]):
            with cols[idx]:
                default_idx = next((i for i, d in enumerate(DAMODARAN_DATA) if d["industry"] == s["industry"]), 0)
                selected_ind = st.selectbox(
                    f"Map: '{s['name']}'",
                    [d["industry"] for d in DAMODARAN_DATA],
                    index=default_idx,
                    key=f"override_select_{s['key']}"
                )
                if selected_ind != s["industry"]:
                    st.session_state.sector_overrides[s["key"]] = selected_ind
                    st.rerun()

        # Display raw table snippet
        if data.get("raw_html_snippet") and data["raw_html_snippet"] != "N/A":
            with st.expander("📄 View SEC Segment Table Raw HTML Snippet"):
                st.code(data["raw_html_snippet"], language="html")
                
        st.markdown("---")

# ----------------- TAB 4: SOCIAL SENTIMENT & RISK -----------------
with tab4:
    render_sentiment_risk_tab(primary_ticker)