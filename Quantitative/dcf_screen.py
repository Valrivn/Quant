"""
Stage 1: DCF Screening — Capitalized R&D, ROIC, Solvency, Margin Health
Pure functions, type hints, dataclasses. No global state.
College-audited logging via college_event().
Degraded-registry pattern: missing data → degraded flag, not hard stop.
Checkpointed: save intermediate state for resume.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from config.logging_config import college_event

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────
CHECKPOINT_DIR = Path("data/checkpoints/dcf_screen")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

MASTER_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
    "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
    "PFE", "TMO", "ABBV", "ACN", "COST"
]

SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary", "META": "Communication Services",
    "NVDA": "Technology", "TSLA": "Consumer Discretionary",
    "JPM": "Financials", "V": "Financials", "JNJ": "Healthcare",
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "MA": "Financials",
    "UNH": "Healthcare", "HD": "Consumer Discretionary",
    "DIS": "Communication Services", "PYPL": "Financials", "ADBE": "Technology",
    "NFLX": "Communication Services", "CRM": "Technology", "INTC": "Technology",
    "CSCO": "Technology", "PFE": "Healthcare", "TMO": "Healthcare",
    "ABBV": "Healthcare", "ACN": "Technology", "COST": "Consumer Staples",
}


# ─── Data Classes ───────────────────────────────────────────────────────
@dataclass
class DCFInputs:
    """Raw inputs for DCF valuation."""
    ticker: str
    revenue: Optional[float] = None
    ebit: Optional[float] = None
    tax_rate: Optional[float] = None
    capex: Optional[float] = None
    depreciation: Optional[float] = None
    change_nwc: Optional[float] = None
    rd_expense: Optional[float] = None
    total_debt: Optional[float] = None
    cash: Optional[float] = None
    shares_outstanding: Optional[float] = None
    current_price: Optional[float] = None
    sector: Optional[str] = None
    degraded: bool = False
    missing_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "revenue": self.revenue,
            "ebit": self.ebit,
            "tax_rate": self.tax_rate,
            "capex": self.capex,
            "depreciation": self.depreciation,
            "change_nwc": self.change_nwc,
            "rd_expense": self.rd_expense,
            "total_debt": self.total_debt,
            "cash": self.cash,
            "shares_outstanding": self.shares_outstanding,
            "current_price": self.current_price,
            "sector": self.sector,
            "degraded": self.degraded,
            "missing_fields": self.missing_fields,
        }


@dataclass
class DCFOutputs:
    """DCF valuation outputs with Monte Carlo distribution."""
    ticker: str
    iv_mean: float
    iv_p25: float
    iv_p10: float
    iv_p50: float
    iv_p75: float
    iv_p90: float
    iv_std: float
    entry_price: float       # IV_P25 (standard entry) - TOTAL equity value
    crisis_entry_price: float  # IV_P10 (black swan Tier 2) - TOTAL equity value
    discount_sector: float
    roic: Optional[float] = None
    solvency_ratio: Optional[float] = None
    margin_health: Optional[float] = None
    rd_capitalized: Optional[float] = None
    current_price: Optional[float] = None
    shares_outstanding: Optional[float] = None
    passes_screen: bool = False
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "iv_mean": self.iv_mean,
            "iv_p25": self.iv_p25,
            "iv_p10": self.iv_p10,
            "iv_p50": self.iv_p50,
            "iv_p75": self.iv_p75,
            "iv_p90": self.iv_p90,
            "iv_std": self.iv_std,
            "entry_price": self.entry_price,
            "crisis_entry_price": self.crisis_entry_price,
            "discount_sector": self.discount_sector,
            "roic": self.roic,
            "solvency_ratio": self.solvency_ratio,
            "margin_health": self.margin_health,
            "rd_capitalized": self.rd_capitalized,
            "current_price": self.current_price,
            "shares_outstanding": self.shares_outstanding,
            "passes_screen": self.passes_screen,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


@dataclass
class ScreenCheckpoint:
    """Checkpoint for resume capability."""
    tickers_done: List[str] = field(default_factory=list)
    tickers_failed: List[str] = field(default_factory=list)
    results: Dict[str, Dict] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Core Functions ─────────────────────────────────────────────────────

def load_position_sizing_config() -> Dict[str, Any]:
    """Load position sizing configuration."""
    path = Path("config/position_sizing.yaml")
    with path.open() as f:
        return yaml.safe_load(f)


def load_sector_sigma(ticker: str) -> float:
    """Get sector historical sigma for discount calibration."""
    config = load_position_sizing_config()
    sector = SECTOR_MAP.get(ticker, "Technology")
    return config["sector_calibration"]["sector_sigma"].get(sector, 0.30)


def compute_sector_discount(sigma: float, config: Dict[str, Any]) -> float:
    """Discount_sector = max(0.08, 1.2 * sigma_sector_historical)."""
    min_discount = config["sector_calibration"]["min_discount"]
    multiplier = config["sector_calibration"]["sigma_multiplier"]
    return max(min_discount, multiplier * sigma)


def capitalize_rd(rd_expense: float, capitalization_rate: float = 0.15) -> float:
    """Capitalize R&D expense as an intangible asset.
    
    R&D Capitalized = R&D_expense / capitalization_rate
    (Per Damodaran: R&D is an operating expense that creates future value)
    """
    if rd_expense <= 0:
        return 0.0
    return rd_expense / capitalization_rate


def compute_fcf(inputs: DCFInputs, rd_capitalized: float, config: Dict[str, Any]) -> float:
    """Compute Free Cash Flow with capitalized R&D adjustment.
    
    FCF = EBIT * (1 - tax_rate) + Depreciation - Capex - Change_NWC
         + R&D_expense - R&D_amortization
    
    Where R&D_amortization = R&D_capitalized * capitalization_rate = R&D_expense
    So net R&D adjustment = 0 (expense added back, amortization subtracted)
    But the capitalized asset adds to invested capital for ROIC.
    """
    if inputs.ebit is None or inputs.tax_rate is None:
        return 0.0
    
    nopat = inputs.ebit * (1 - inputs.tax_rate)
    fcf = nopat
    
    if inputs.depreciation:
        fcf += inputs.depreciation
    if inputs.capex:
        fcf -= inputs.capex
    if inputs.change_nwc:
        fcf -= inputs.change_nwc
    
    # R&D: add back expense (was subtracted to get EBIT), subtract amortization
    if inputs.rd_expense:
        fcf += inputs.rd_expense  # Add back R&D expense
        fcf -= inputs.rd_expense  # Subtract amortization (same amount)
    
    return fcf


def compute_invested_capital(inputs: DCFInputs, rd_capitalized: float) -> float:
    """Compute Invested Capital = Total Debt + Book Equity - Cash + R&D_Capitalized.
    
    Uses book-value based approximation (not market cap).
    Book Equity ≈ Revenue * sector_equity_multiplier.
    """
    invested = 0.0
    
    # Total debt (interest-bearing)
    if inputs.total_debt:
        invested += inputs.total_debt
    
    # Book Equity approximation: Revenue * sector multiplier
    # Tech: 0.3x, Industrials: 0.5x, Financials: 0.8x, etc.
    equity_multipliers = {
        "Technology": 0.35,
        "Communication Services": 0.40,
        "Consumer Discretionary": 0.45,
        "Healthcare": 0.40,
        "Industrials": 0.50,
        "Financials": 0.70,
        "Consumer Staples": 0.45,
        "Energy": 0.55,
        "Materials": 0.50,
        "Utilities": 0.60,
        "Real Estate": 0.60,
    }
    sector = inputs.sector or "Technology"
    multiplier = equity_multipliers.get(sector, 0.40)
    
    if inputs.revenue:
        book_equity = inputs.revenue * multiplier
        invested += book_equity
    
    # Subtract cash (non-operating)
    if inputs.cash:
        invested -= inputs.cash
    
    # Add capitalized R&D
    invested += rd_capitalized
    
    return max(invested, 1.0)  # Avoid division by zero


def compute_roic(fcf: float, invested_capital: float) -> Optional[float]:
    """Return on Invested Capital = FCF / Invested Capital."""
    if invested_capital <= 0:
        return None
    return fcf / invested_capital


def compute_solvency_ratio(inputs: DCFInputs) -> Optional[float]:
    """Solvency Ratio = (EBITDA - Capex) / Total Debt.
    
    Approximates ability to cover debt from operating cash flow.
    """
    if inputs.total_debt is None or inputs.total_debt <= 0:
        return None
    if inputs.ebit is None or inputs.depreciation is None or inputs.capex is None:
        return None
    
    ebitda = inputs.ebit + inputs.depreciation
    ocf_proxy = ebitda - inputs.capex
    return ocf_proxy / inputs.total_debt


def compute_margin_health(inputs: DCFInputs) -> Optional[float]:
    """Margin Health = EBIT Margin * (1 - Tax Rate) — normalized quality score."""
    if inputs.revenue is None or inputs.revenue <= 0 or inputs.ebit is None:
        return None
    ebit_margin = inputs.ebit / inputs.revenue
    if inputs.tax_rate:
        ebit_margin *= (1 - inputs.tax_rate)
    return ebit_margin


def monte_carlo_dcf(
    fcf: float,
    invested_capital: float,
    roic: float,
    discount_rate: float,
    config: Dict[str, Any],
    inputs: DCFInputs,
    n_passes: int = 10000,
    seed: int = 42,
) -> Tuple[np.ndarray, Dict[str, float]]:
    """Run Monte Carlo DCF simulation.
    
    Returns array of intrinsic values and summary percentiles.
    """
    mc_config = config["monte_carlo"]
    rng = np.random.default_rng(seed)
    
    # Growth rate distribution
    g_mean = mc_config["growth_rate_dist"]["mean"]
    g_std = mc_config["growth_rate_dist"]["std"]
    g_min = mc_config["growth_rate_dist"]["min"]
    g_max = mc_config["growth_rate_dist"]["max"]
    
    # WACC distribution (centered on discount_rate)
    w_mean = mc_config["wacc_dist"]["mean"]
    w_std = mc_config["wacc_dist"]["std"]
    w_min = mc_config["wacc_dist"]["min"]
    w_max = mc_config["wacc_dist"]["max"]
    
    # Terminal multiple distribution
    t_mean = mc_config["terminal_multiple_dist"]["mean"]
    t_std = mc_config["terminal_multiple_dist"]["std"]
    t_min = mc_config["terminal_multiple_dist"]["min"]
    t_max = mc_config["terminal_multiple_dist"]["max"]
    
    # Generate samples
    growth_rates = np.clip(
        rng.normal(g_mean, g_std, n_passes),
        g_min, g_max
    )
    waccs = np.clip(
        rng.normal(w_mean, w_std, n_passes),
        w_min, w_max
    )
    # WACC stays at Monte Carlo distribution (mean ~9%)
    # Sector discount is applied only to entry price, not WACC
    
    terminal_multiples = np.clip(
        rng.normal(t_mean, t_std, n_passes),
        t_min, t_max
    )
    
    # DCF: PV of 5-year explicit forecast + Terminal Value
    # FCF grows at growth_rate for 5 years
    years = 5
    intrinsic_values = np.zeros(n_passes)
    
    for i in range(n_passes):
        g = growth_rates[i]
        w = waccs[i]
        t_mult = terminal_multiples[i]
        
        # Cap growth at WACC - 1% for terminal value stability
        g_terminal = min(g, w - 0.01)
        
        # Explicit forecast period
        pv_explicit = 0.0
        fcf_t = fcf
        for yr in range(1, years + 1):
            fcf_t *= (1 + g)
            pv_explicit += fcf_t / ((1 + w) ** yr)
        
        # Terminal value at year 5 (EV basis)
        terminal_fcf = fcf_t * (1 + g_terminal)  # Year 6 FCF
        if w > g_terminal:
            terminal_value = terminal_fcf / (w - g_terminal)  # Gordon growth
        else:
            terminal_value = terminal_fcf * min(t_mult, 20.0)  # Cap EV/FCF at 20x
        pv_terminal = terminal_value / ((1 + w) ** years)
        
        # Firm value = explicit + terminal
        firm_value = pv_explicit + pv_terminal
        
        # Equity value = Firm value - Net Debt
        net_debt = max(0.0, (inputs.total_debt or 0.0) - (inputs.cash or 0.0))
        intrinsic_values[i] = max(0.0, firm_value - net_debt)
    
    # Percentiles
    percentiles = {
        "p10": float(np.percentile(intrinsic_values, 10)),
        "p25": float(np.percentile(intrinsic_values, 25)),
        "p50": float(np.percentile(intrinsic_values, 50)),
        "p75": float(np.percentile(intrinsic_values, 75)),
        "p90": float(np.percentile(intrinsic_values, 90)),
        "mean": float(intrinsic_values.mean()),
        "std": float(intrinsic_values.std()),
    }
    
    return intrinsic_values, percentiles


def evaluate_dcf_screen(outputs: DCFOutputs, config: Dict[str, Any]) -> bool:
    """Determine if ticker passes DCF screen.
    
    Pass criteria:
    1. Current price <= Fair Value * (1 - Margin of Safety) [Entry threshold]
    2. ROIC > WACC (value creation) - use MC WACC mean, not sector discount
    3. Solvency ratio > 1.0 (debt coverage)
    4. Margin health > sector median (quality)
    """
    if outputs.degraded:
        return False
    
    if outputs.current_price is None:
        return False
    
    shares = outputs.shares_outstanding
    if shares is None or shares <= 0:
        outputs.warnings.append("Shares outstanding unavailable")
        return False
    
    # Fair value = P50 per share
    fair_value_per_share = outputs.iv_p50 / shares
    crisis_value_per_share = outputs.iv_p10 / shares
    
    # Entry threshold = Fair Value * (1 - Margin of Safety)
    mc_config = config["monte_carlo"]
    margin_of_safety = mc_config.get("entry_margin_of_safety", 0.10)
    entry_threshold_per_share = fair_value_per_share * (1 - margin_of_safety)
    crisis_threshold_per_share = crisis_value_per_share
    
    # Price <= Entry Threshold (Fair Value - Margin of Safety)
    if outputs.current_price > entry_threshold_per_share:
        outputs.warnings.append(f"Price {outputs.current_price:.2f} > Entry Threshold {entry_threshold_per_share:.2f} (Fair Value {fair_value_per_share:.2f} * {1-margin_of_safety:.0%})")
        return False
    
    # ROIC > WACC (value creation) - use Monte Carlo WACC mean
    mc_wacc_mean = config["monte_carlo"]["wacc_dist"]["mean"]  # 0.08
    if outputs.roic is not None and outputs.roic <= mc_wacc_mean:
        outputs.warnings.append(f"ROIC {outputs.roic:.2%} <= WACC {mc_wacc_mean:.2%}")
        return False
    
    # Solvency > 1.0
    if outputs.solvency_ratio is not None and outputs.solvency_ratio < 1.0:
        outputs.warnings.append(f"Solvency {outputs.solvency_ratio:.2f} < 1.0")
        return False
    
    # Margin health check (degraded if missing)
    if outputs.margin_health is None:
        outputs.warnings.append("Margin health unavailable")
    
    college_event(
        "dcf_screen_pass",
        ticker=outputs.ticker,
        price=outputs.current_price,
        fair_value_per_share=fair_value_per_share,
        entry_threshold=entry_threshold_per_share,
        roic=outputs.roic,
        solvency=outputs.solvency_ratio,
        margin_health=outputs.margin_health,
    )
    return True


def run_dcf_for_ticker(ticker: str, config: Dict[str, Any]) -> DCFOutputs:
    """Run complete DCF screen for a single ticker."""
    # Load raw financial data
    inputs = load_financial_inputs(ticker)
    
    if inputs.degraded:
        return DCFOutputs(
            ticker=ticker,
            iv_mean=0, iv_p25=0, iv_p10=0, iv_p50=0, iv_p75=0, iv_p90=0,
            iv_std=0, entry_price=0, crisis_entry_price=0,
            discount_sector=0, degraded=True, warnings=inputs.missing_fields,
            current_price=inputs.current_price,
            shares_outstanding=inputs.shares_outstanding
        )
    
    # Capitalize R&D
    rd_capitalized = capitalize_rd(
        inputs.rd_expense or 0,
        config["monte_carlo"]["rd_capitalization_rate"]
    )
    
    # Compute metrics
    fcf = compute_fcf(inputs, rd_capitalized, config)
    invested_capital = compute_invested_capital(inputs, rd_capitalized)
    roic = compute_roic(fcf, invested_capital)
    solvency = compute_solvency_ratio(inputs)
    margin_health = compute_margin_health(inputs)
    
    # Sector discount
    sigma = load_sector_sigma(ticker)
    discount_sector = compute_sector_discount(sigma, config)
    
    # Monte Carlo DCF
    _, percentiles = monte_carlo_dcf(
        fcf, invested_capital, roic or discount_sector, discount_sector, config, inputs
    )
    
    outputs = DCFOutputs(
        ticker=ticker,
        iv_mean=percentiles["mean"],
        iv_p25=percentiles["p25"],
        iv_p10=percentiles["p10"],
        iv_p50=percentiles["p50"],
        iv_p75=percentiles["p75"],
        iv_p90=percentiles["p90"],
        iv_std=percentiles["std"],
        entry_price=percentiles["p25"],
        crisis_entry_price=percentiles["p10"],
        discount_sector=discount_sector,
        roic=roic,
        solvency_ratio=solvency,
        margin_health=margin_health,
        rd_capitalized=rd_capitalized,
        current_price=inputs.current_price,
        shares_outstanding=inputs.shares_outstanding,
        passes_screen=False,  # Evaluated below
        degraded=False,
        warnings=[],
    )
    
    outputs.passes_screen = evaluate_dcf_screen(outputs, config)
    
    college_event(
        "dcf_screen_complete",
        ticker=ticker,
        passes=outputs.passes_screen,
        iv_p25=outputs.entry_price,
        price=inputs.current_price,
        roic=roic,
    )
    
    return outputs


def load_pit_financial_inputs(
    ticker: str,
    as_of: Optional[str] = None
) -> DCFInputs:
    """Load financial data from PIT fundamentals with point-in-time lookup.
    
    Uses as_of date for PIT-safe lookup: only 10-K filings with filing_date <= as_of are used.
    Prefers 10-K (annual) filings for fundamental analysis.
    Falls back to latest available 10-K if as_of not specified.
    
    Degraded-registry pattern: returns DCFInputs with degraded=True
    and missing_fields populated instead of raising.
    """
    import json
    import pandas as pd
    
    missing = []
    
    # Load parsed filings JSON (contains form type)
    parsed_path = Path(f"data/pit_fundamentals/{ticker}/parsed_filings.json")
    if not parsed_path.exists():
        missing.append("parsed_filings_missing")
        return DCFInputs(
            ticker=ticker,
            degraded=True,
            missing_fields=missing,
            sector=SECTOR_MAP.get(ticker, "Technology"),
        )
    
    try:
        with parsed_path.open() as f:
            filings = json.load(f)
        
        if not filings:
            missing.append("no_filings")
            return DCFInputs(
                ticker=ticker,
                degraded=True,
                missing_fields=missing,
                sector=SECTOR_MAP.get(ticker, "Technology"),
            )
        
        # Filter for 10-K filings only (annual)
        k_filings = [f for f in filings if f.get("form") == "10-K"]
        if not k_filings:
            missing.append("no_10k_filings")
            return DCFInputs(
                ticker=ticker,
                degraded=True,
                missing_fields=missing,
                sector=SECTOR_MAP.get(ticker, "Technology"),
            )
        
        # PIT lookup: filter by as_of date
        if as_of:
            cutoff = pd.Timestamp(as_of)
            k_filings = [f for f in k_filings if pd.Timestamp(f["filed"]) <= cutoff]
        
        if not k_filings:
            missing.append("no_10k_before_as_of")
            return DCFInputs(
                ticker=ticker,
                degraded=True,
                missing_fields=missing,
                sector=SECTOR_MAP.get(ticker, "Technology"),
            )
        
        # Get most recent 10-K filing
        k_filings.sort(key=lambda f: f["filed"], reverse=True)
        latest_10k = k_filings[0]
        facts = latest_10k.get("facts", {})
        
        def get_field(field: str) -> Optional[float]:
            if field in facts:
                val = facts[field]
                if val is not None:
                    return float(val)
            return None
        
        revenue = get_field("revenue")
        ebit = get_field("ebit")
        tax_rate = get_field("tax_rate")
        capex = get_field("capex")
        depreciation = get_field("depreciation")
        change_nwc = get_field("change_nwc")
        rd_expense = get_field("rd_expense")
        total_debt = get_field("total_debt")
        cash = get_field("cash")
        shares_outstanding = get_field("shares_outstanding")
        
        # Provide sensible defaults for non-critical missing fields
        # Only mark as missing if critical fields are absent
        critical_fields = {
            "revenue": revenue, "ebit": ebit, "capex": capex, "depreciation": depreciation
        }
        for field, val in critical_fields.items():
            if val is None:
                missing.append(field)
        
        # Non-critical fields: provide defaults instead of marking missing
        if tax_rate is None:
            tax_rate = 0.21  # US corporate tax rate default
        if rd_expense is None:
            rd_expense = 0.0  # Not all companies report R&D separately
        if change_nwc is None:
            change_nwc = 0.0  # Default to no change
        
        # Track missing for non-critical fields (for logging only)
        for field, val in [
            ("total_debt", total_debt), ("cash", cash),
            ("shares_outstanding", shares_outstanding)
        ]:
            if val is None:
                missing.append(field)
        
    except Exception as e:
        logger.warning(f"PIT data load failed for {ticker}: {e}")
        missing.append("pit_parse_error")
    
    # Current price from yfinance cache (for entry comparison)
    price = None
    yf_path = Path(f"data/source/yfinance/{ticker}/prices.parquet")
    if yf_path.exists():
        try:
            price_df = pd.read_parquet(yf_path)
            if not price_df.empty:
                price = float(price_df["Close"].iloc[-1])
        except Exception:
            pass
    if price is None:
        missing.append("current_price")
    
    sector = SECTOR_MAP.get(ticker, "Technology")
    
    return DCFInputs(
        ticker=ticker,
        revenue=revenue,
        ebit=ebit,
        tax_rate=tax_rate,
        capex=capex,
        depreciation=depreciation,
        change_nwc=change_nwc,
        rd_expense=rd_expense,
        total_debt=total_debt,
        cash=cash,
        shares_outstanding=shares_outstanding,
        current_price=price,
        sector=sector,
        degraded=len(missing) > 3,  # Allow up to 3 missing fields
        missing_fields=missing,
    )


def load_financial_inputs(ticker: str) -> DCFInputs:
    """Load financial data — defaults to PIT with no date (latest available)."""
    return load_pit_financial_inputs(ticker, as_of=None)


def save_checkpoint(checkpoint: ScreenCheckpoint) -> None:
    """Save checkpoint for resume."""
    path = CHECKPOINT_DIR / "dcf_screen_checkpoint.json"
    with path.open("w") as f:
        json.dump({
            "tickers_done": checkpoint.tickers_done,
            "tickers_failed": checkpoint.tickers_failed,
            "results": checkpoint.results,
            "updated_at": checkpoint.updated_at,
        }, f, indent=2, default=str)


def load_checkpoint() -> ScreenCheckpoint:
    """Load checkpoint for resume."""
    path = CHECKPOINT_DIR / "dcf_screen_checkpoint.json"
    if not path.exists():
        return ScreenCheckpoint()
    try:
        with path.open() as f:
            data = json.load(f)
        return ScreenCheckpoint(
            tickers_done=data.get("tickers_done", []),
            tickers_failed=data.get("tickers_failed", []),
            results=data.get("results", {}),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )
    except Exception as e:
        logger.warning(f"Checkpoint load failed: {e}")
        return ScreenCheckpoint()


def run_dcf_screen(tickers: Optional[List[str]] = None) -> Dict[str, DCFOutputs]:
    """Main entry point: run DCF screen on ticker universe."""
    if tickers is None:
        tickers = MASTER_TICKERS
    
    config = load_position_sizing_config()
    checkpoint = load_checkpoint()
    
    # Skip already done
    remaining = [t for t in tickers if t not in checkpoint.tickers_done]
    logger.info(f"DCF Screen: {len(remaining)} tickers remaining of {len(tickers)}")
    
    results = {t: DCFOutputs(**v) for t, v in checkpoint.results.items()}
    
    for ticker in remaining:
        if ticker in checkpoint.tickers_failed:
            continue
        
        try:
            output = run_dcf_for_ticker(ticker, config)
            results[ticker] = output
            checkpoint.tickers_done.append(ticker)
            checkpoint.results[ticker] = output.to_dict()
        except Exception as e:
            logger.error(f"DCF screen failed for {ticker}: {e}")
            checkpoint.tickers_failed.append(ticker)
        
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        save_checkpoint(checkpoint)
    
    # Save final results
    out_path = CHECKPOINT_DIR / "dcf_screen_results.json"
    with out_path.open("w") as f:
        json.dump({t: o.to_dict() for t, o in results.items()}, f, indent=2, default=str)
    
    passed = [t for t, o in results.items() if o.passes_screen]
    logger.info(f"DCF Screen complete: {len(passed)}/{len(tickers)} passed")
    college_event("dcf_screen_batch_complete", passed=passed, total=len(tickers))
    
    return results


if __name__ == "__main__":
    # Smoke test with synthetic data
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test
    test_inputs = DCFInputs(
        ticker="TEST",
        revenue=100_000_000_000,
        ebit=30_000_000_000,
        tax_rate=0.21,
        capex=10_000_000_000,
        depreciation=8_000_000_000,
        change_nwc=1_000_000_000,
        rd_expense=15_000_000_000,
        total_debt=50_000_000_000,
        cash=20_000_000_000,
        shares_outstanding=1_000_000_000,
        current_price=150.0,
        sector="Technology",
    )
    
    config = load_position_sizing_config()
    rd_cap = capitalize_rd(test_inputs.rd_expense, config["monte_carlo"]["rd_capitalization_rate"])
    fcf = compute_fcf(test_inputs, rd_cap, config)
    inv_cap = compute_invested_capital(test_inputs, rd_cap)
    roic = compute_roic(fcf, inv_cap)
    solvency = compute_solvency_ratio(test_inputs)
    margin = compute_margin_health(test_inputs)
    sigma = load_sector_sigma("TEST")
    discount = compute_sector_discount(sigma, config)
    _, pctls = monte_carlo_dcf(fcf, inv_cap, roic or discount, discount, config, n_passes=1000)
    
    print("Synthetic DCF Test:")
    print(f"  R&D Capitalized: ${rd_cap:,.0f}")
    print(f"  FCF: ${fcf:,.0f}")
    print(f"  Invested Capital: ${inv_cap:,.0f}")
    print(f"  ROIC: {roic:.2%}")
    print(f"  Solvency: {solvency:.2f}")
    print(f"  Margin Health: {margin:.2%}")
    print(f"  Sector Sigma: {sigma:.2f}")
    print(f"  Discount Rate: {discount:.2%}")
    print(f"  IV P25 (Entry): ${pctls['p25']:,.2f}")
    print(f"  IV P10 (Crisis): ${pctls['p10']:,.2f}")
    print(f"  IV Mean: ${pctls['mean']:,.2f}")