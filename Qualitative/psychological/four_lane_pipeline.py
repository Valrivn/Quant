import asyncio
import logging
import sqlite3
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import load_hybrid_config
from psychological.dcf_floor import DCFFloorClient, DCFFloorOutput, create_dcf_floor_client
from psychological.qualitative_scoring import (
    SubSectorConfig,
    FinancialReconstructionInterface,
    FinancialReconstructionResult,
    TrajectoryCorridorEngine,
    TrajectoryCorridorResult,
    MoatComposite,
    CultureComposite,
    HypeComposite,
    DoubleStandardizer,
    LaneAlphaPipeline,
    LaneAlphaResult,
    AlternativeStrategyPipeline,
    PipelineOutput,
)
from psychological.monte_carlo import (
    MonteCarloEngine,
    MonteCarloResult,
    MonteCarloInput,
    create_monte_carlo_engine,
)
from psychological.signal_matrix import SignalMatrix, create_signal_matrix
from psychological.scrapers.cross_validation import (
    CrossValidationEngine,
    create_cross_validation_engine,
)
from psychological.engineering_guards import guard_nan, clamp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthetic Bond Rating — Damodaran's Interest Coverage Ratio mapping
# Maps ICR to a synthetic credit rating, then derives pre-tax cost of debt.
# ---------------------------------------------------------------------------

ICR_TO_RATING: list[tuple[float, str, float]] = [
    (12.5, "AAA", 0.0063),
    (9.5,  "AA",  0.0075),
    (7.5,  "A+",  0.0090),
    (6.0,  "A",   0.0105),
    (4.5,  "A-",  0.0120),
    (4.0,  "BBB", 0.0150),
    (3.5,  "BB+", 0.0200),
    (3.0,  "BB",  0.0250),
    (2.5,  "B+",  0.0325),
    (2.0,  "B",   0.0400),
    (1.5,  "B-",  0.0525),
    (1.0,  "CCC", 0.0650),
    (0.5,  "CC",  0.0850),
    (0.0,  "C",   0.1000),
]


def synthetic_bond_rating(icr: float) -> tuple[str, float]:
    if icr <= 0:
        return "D", 0.12
    for threshold, rating, spread in ICR_TO_RATING:
        if icr >= threshold:
            return rating, spread
    return "C", 0.10


def compute_cost_of_debt(icr: float, risk_free_rate: float = 0.045) -> float:
    _, spread = synthetic_bond_rating(icr)
    return risk_free_rate + spread


def compute_wacc(
    equity_weight: float, cost_of_equity: float,
    debt_weight: float, cost_of_debt: float,
    tax_rate: float = 0.21,
) -> float:
    after_tax_debt = cost_of_debt * (1 - tax_rate)
    return equity_weight * cost_of_equity + debt_weight * after_tax_debt


# ---------------------------------------------------------------------------
# Qualitative Modulating Coefficients — decoupled from quantitative inputs
# ---------------------------------------------------------------------------

def modulate_wacc_with_qualitative(
    base_wacc: float,
    culture_score: float | None,
    hype_score: float | None,
    moat_strength: float = 0.5,
) -> float:
    composite = 0.0
    n = 0
    if culture_score is not None:
        composite += culture_score
        n += 1
    if hype_score is not None:
        composite += 1.0 - hype_score  # inverse: high hype → higher risk premium
        n += 1
    if n == 0:
        adj = 0.5
    else:
        adj = composite / n
    modulation = (moat_strength * 0.4 + adj * 0.6) * 0.02
    return base_wacc - modulation


def modulate_cap_horizon(culture_score: float | None, moat_strength: float = 0.5) -> int:
    if culture_score is None:
        culture_score = 0.5
    blended = culture_score * 0.5 + moat_strength * 0.5
    extra_years = int(round(blended * 8))
    return max(1, extra_years)

TICKERS = ["NVDA", "AMD", "INTC", "AVGO", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]

FUNDAMENTAL_ESTIMATES: Dict[str, Dict[str, float]] = {
    "NVDA":  {"revenue": 130_000_000_000, "fcf": 65_000_000_000,  "roic": 0.45, "wacc": 0.11, "rr": 0.55, "op_margin": 0.55, "sector": "semiconductor",     "icr": 35.0,  "ebit": 60_000_000_000,  "interest_expense": 1_700_000_000, "geo_stress": 0.25, "geo_prem_rate": 0.0284},
    "AMD":   {"revenue": 25_000_000_000,  "fcf": 5_000_000_000,   "roic": 0.15, "wacc": 0.12, "rr": 0.40, "op_margin": 0.20, "sector": "semiconductor",     "icr": 15.0,  "ebit": 5_000_000_000,   "interest_expense": 330_000_000, "geo_stress": 0.10, "geo_prem_rate": 0.015},
    "INTC":  {"revenue": 54_000_000_000,  "fcf": -8_000_000_000,  "roic": 0.03, "wacc": 0.13, "rr": 0.60, "op_margin": 0.05, "sector": "semiconductor",     "icr": 3.5,   "ebit": 2_700_000_000,  "interest_expense": 770_000_000, "geo_stress": 0.05, "geo_prem_rate": 0.01},
    "AVGO":  {"revenue": 50_000_000_000,  "fcf": 25_000_000_000,  "roic": 0.30, "wacc": 0.10, "rr": 0.50, "op_margin": 0.40, "sector": "semiconductor",     "icr": 22.0,  "ebit": 20_000_000_000,  "interest_expense": 910_000_000, "geo_stress": 0.12, "geo_prem_rate": 0.018},
    "MSFT":  {"revenue": 245_000_000_000, "fcf": 82_000_000_000,  "roic": 0.30, "wacc": 0.09, "rr": 0.45, "op_margin": 0.42, "sector": "platform_software", "icr": 40.0,  "ebit": 103_000_000_000, "interest_expense": 2_600_000_000, "geo_stress": 0.0, "geo_prem_rate": 0.0},
    "GOOGL": {"revenue": 340_000_000_000, "fcf": 86_000_000_000,  "roic": 0.25, "wacc": 0.09, "rr": 0.40, "op_margin": 0.30, "sector": "platform_software", "icr": 30.0,  "ebit": 102_000_000_000, "interest_expense": 3_400_000_000, "geo_stress": 0.0, "geo_prem_rate": 0.0},
    "META":  {"revenue": 165_000_000_000, "fcf": 62_000_000_000,  "roic": 0.22, "wacc": 0.10, "rr": 0.35, "op_margin": 0.35, "sector": "platform_software", "icr": 28.0,  "ebit": 58_000_000_000,  "interest_expense": 2_100_000_000, "geo_stress": 0.0, "geo_prem_rate": 0.0},
    "TSLA":  {"revenue": 97_000_000_000,  "fcf": 5_000_000_000,   "roic": 0.12, "wacc": 0.14, "rr": 0.50, "op_margin": 0.10, "sector": "hardware_oem",      "icr": 8.0,   "ebit": 9_700_000_000,   "interest_expense": 1_200_000_000, "geo_stress": 0.05, "geo_prem_rate": 0.008},
    "AAPL":  {"revenue": 395_000_000_000, "fcf": 115_000_000_000, "roic": 0.40, "wacc": 0.09, "rr": 0.35, "op_margin": 0.32, "sector": "hardware_oem",      "icr": 25.0,  "ebit": 126_000_000_000, "interest_expense": 5_000_000_000, "geo_stress": 0.03, "geo_prem_rate": 0.005},
    "AMZN":  {"revenue": 620_000_000_000, "fcf": 64_000_000_000,  "roic": 0.15, "wacc": 0.10, "rr": 0.30, "op_margin": 0.12, "sector": "hardware_oem",      "icr": 12.0,  "ebit": 74_000_000_000,  "interest_expense": 6_200_000_000, "geo_stress": 0.03, "geo_prem_rate": 0.005},
}

CURRENT_PRICES: Dict[str, float] = {
    "NVDA": 130.0, "AMD": 140.0, "INTC": 22.0, "AVGO": 175.0,
    "MSFT": 450.0, "GOOGL": 185.0, "META": 520.0, "TSLA": 220.0,
    "AAPL": 225.0, "AMZN": 195.0,
}


@dataclass
class Lane1Result:
    ticker: str
    dcf_output: Optional[DCFFloorOutput]
    financial_reconstruction: Optional[FinancialReconstructionResult]
    trajectory: Optional[TrajectoryCorridorResult]
    expected_growth: float
    reinvestment_rate: float
    roic: float
    subsector: Optional[str]
    synthetic_rating: str = ""
    synthetic_spread: float = 0.0
    cost_of_debt: float = 0.0
    modulated_wacc: float = 0.0
    cap_horizon_years: int = 5
    qualitative_modulation_applied: bool = False
    culture_score: float = 0.5
    supplier_concentration: float = 0.5
    moat_score: float = 0.5
    a_tech: float = 0.0
    geopolitical_stress_factor: float = 0.0
    geopolitical_risk_premium_rate: float = 0.0


@dataclass
class Lane2Result:
    ticker: str
    hype_score: Optional[float]
    culture_score: Optional[float]
    blended_score: Optional[float]
    stage1_z: Optional[float]
    stage2_z: Optional[float]
    pricing_deviation: Optional[float]
    justified_multiple: Optional[float]
    actual_multiple: Optional[float]
    high_conviction_catalyst: bool = False


@dataclass
class Lane3Result:
    ticker: str
    monte_carlo: MonteCarloResult
    positive_eva_prob: float
    macro_risk_adjustment: float
    confidence_band: str
    survival_probability: float = 1.0
    catastrophe_event_count: int = 0
    geopolitical_wacc_premium: float = 0.0
    displacement_ratio: float = 0.0
    is_leader: bool = False


@dataclass
class Lane4Result:
    ticker: str
    anti_contamination_passed: bool
    hardcoding_guard_passed: bool
    spearman_ic: Optional[float]
    terminal_stability_passed: bool
    validation_layers: Dict[str, Any]
    overall_passed: bool


@dataclass
class FourLaneOutput:
    ticker: str
    lane1: Lane1Result
    lane2: Lane2Result
    lane3: Lane3Result
    lane4: Lane4Result
    conviction_label: str
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _get_subsector_regression_params(subsector: Optional[str]) -> Dict[str, str]:
    params = {
        "semiconductor": {
            "multiple": "EV/Invested Capital",
            "dependent": "ROIC",
        },
        "platform_software": {
            "multiple": "EV/Sales",
            "dependent": "After-Tax Operating Margin",
        },
        "hardware_oem": {
            "multiple": "Price-to-Book",
            "dependent": "ROE",
        },
    }
    return params.get(subsector, {})


class FourLanePipeline:
    def __init__(
        self,
        db_path: str = "reddit_quant.db",
        config_dict: Optional[dict] = None,
    ):
        self.db_path = db_path
        self.config = config_dict or load_hybrid_config()
        self.dcf_client = create_dcf_floor_client(db_path, self.config)
        self.mc_engine = create_monte_carlo_engine(self.config)
        self.cross_val = create_cross_validation_engine(self.config)
        self.signal_matrix = create_signal_matrix(self.config)
        self.subsector_cfg = SubSectorConfig.from_config(self.config)
        self.fin_recon = FinancialReconstructionInterface(self.config)
        self.trajectory = TrajectoryCorridorEngine()
        self.lane_alpha = LaneAlphaPipeline()
        self.alternative = AlternativeStrategyPipeline(self.fin_recon, self.trajectory)

    async def run_lane1(
        self,
        tickers: List[str],
        qualitative_modulators: Optional[Dict[str, Dict[str, Optional[float]]]] = None,
    ) -> Dict[str, Lane1Result]:
        results: Dict[str, Lane1Result] = {}
        for ticker in tickers:
            est = FUNDAMENTAL_ESTIMATES.get(ticker, {})
            price = CURRENT_PRICES.get(ticker, 100.0)
            revenue = est.get("revenue", 10_000_000_000)
            fcf = est.get("fcf", 1_000_000_000)
            roic = est.get("roic", 0.10)
            wacc = est.get("wacc", 0.10)
            rr = est.get("rr", 0.35)
            op_margin = est.get("op_margin", 0.15)
            sector = est.get("sector", "software")
            icr = est.get("icr", 5.0)

            expected_growth = rr * roic

            rating, spread = synthetic_bond_rating(icr)
            risk_free = 0.045
            cost_of_debt = compute_cost_of_debt(icr, risk_free)

            equity_weight = 0.75
            debt_weight = 0.25
            cost_of_equity = risk_free + 0.055
            modulated_wacc = compute_wacc(equity_weight, cost_of_equity, debt_weight, cost_of_debt)

            qualitative_applied = False
            culture_val = 0.5
            supplier_concentration_val = 0.5
            moat_score_val = 0.5
            a_tech_val = 0.0
            geo_stress_val = est.get("geo_stress", 0.0)
            geo_prem_val = est.get("geo_prem_rate", 0.0)

            if qualitative_modulators and ticker in qualitative_modulators:
                q = qualitative_modulators[ticker]
                culture_val = q.get("culture_score") if q.get("culture_score") is not None else 0.5
                moat_score_val = q.get("moat_strength") if q.get("moat_strength") is not None else (q.get("moat_score") if q.get("moat_score") is not None else 0.5)
                supplier_concentration_val = q.get("supplier_concentration") if q.get("supplier_concentration") is not None else 0.5
                a_tech_val = q.get("a_tech") if q.get("a_tech") is not None else 0.0

                modulated_wacc = modulate_wacc_with_qualitative(
                    modulated_wacc, culture_val, q.get("hype_score"), moat_score_val
                )
                qualitative_applied = True

            wacc = clamp(modulated_wacc, 0.03, 0.20)

            dcf_output = self.dcf_client.fetch_and_store(
                ticker=ticker,
                current_price=price,
                fcf_projection=fcf,
                wacc=wacc,
            )

            fin_recon_output: Optional[FinancialReconstructionResult] = None
            if revenue > 0:
                fin_recon_output = self.fin_recon.evaluate(
                    ticker=ticker,
                    rd_expense=revenue * 0.15,
                    revenue=revenue,
                    gross_profit=revenue * 0.60,
                    sbc_expense=fcf * 0.10,
                    shares_outstanding=1_000_000_000,
                    share_price=price,
                    sector=sector,
                    operating_margin=op_margin,
                )

            cap_horizon = 5
            if qualitative_applied:
                cap_horizon = 5 + modulate_cap_horizon(culture_val, moat_score_val)

            traj_output: Optional[TrajectoryCorridorResult] = None
            if fin_recon_output:
                z_score = (roic - wacc) / 0.10
                traj_output = self.trajectory.compute(ticker, z_score)

            subsector = self.subsector_cfg.get_subsector_for_ticker(ticker)

            results[ticker] = Lane1Result(
                ticker=ticker,
                dcf_output=dcf_output,
                financial_reconstruction=fin_recon_output,
                trajectory=traj_output,
                expected_growth=expected_growth,
                reinvestment_rate=rr,
                roic=roic,
                subsector=subsector,
                synthetic_rating=rating,
                synthetic_spread=spread,
                cost_of_debt=cost_of_debt,
                modulated_wacc=wacc,
                cap_horizon_years=cap_horizon,
                qualitative_modulation_applied=qualitative_applied,
                culture_score=culture_val,
                supplier_concentration=supplier_concentration_val,
                moat_score=moat_score_val,
                a_tech=a_tech_val,
                geopolitical_stress_factor=geo_stress_val,
                geopolitical_risk_premium_rate=geo_prem_val,
            )
        return results

    def run_lane2(
        self,
        tickers: List[str],
        lane1_results: Optional[Dict[str, Lane1Result]] = None,
    ) -> Dict[str, Lane2Result]:
        results: Dict[str, Lane2Result] = {}
        for ticker in tickers:
            est = FUNDAMENTAL_ESTIMATES.get(ticker, {})
            price = CURRENT_PRICES.get(ticker, 100.0)
            revenue = est.get("revenue", 10_000_000_000)
            roic = est.get("roic", 0.10)
            wacc = est.get("wacc", 0.10)
            op_margin = est.get("op_margin", 0.15)

            subsector = self.subsector_cfg.get_subsector_for_ticker(ticker)

            culture_signals = {
                "employee_sentiment": 0.6 + (roic - 0.10) * 2.0,
                "hiring_velocity": 0.5 + (roic - 0.10) * 1.5,
                "dev_velocity": 0.5 + (roic - 0.10) * 3.0,
                "product_sentiment": 0.6 + (op_margin - 0.10) * 1.0,
            }
            hype_signals = {
                "reddit_velocity": 0.5 + (roic - 0.10) * 2.0,
                "bull_bear_ratio": 1.5 + (roic - 0.10) * 5.0,
                "mention_velocity": 0.5 + (roic - 0.10) * 2.0,
                "social_sentiment": 0.5 + (roic - 0.10) * 1.5,
            }

            hype_signals = {k: guard_nan(v, 0.5) for k, v in hype_signals.items()}
            culture_signals = {k: guard_nan(v, 0.5) for k, v in culture_signals.items()}

            self.lane_alpha.ingest_culture(ticker, culture_signals)
            self.lane_alpha.ingest_hype(ticker, hype_signals)
            alpha_result: LaneAlphaResult = self.lane_alpha.run(ticker, culture_signals, hype_signals)

            sector_params = _get_subsector_regression_params(subsector)
            if subsector == "platform_software":
                ev_sales = (price * 10_000_000_000) / revenue if revenue > 0 else 0
                justified_ev = op_margin * 25
                pricing_dev = ev_sales - justified_ev
                actual_mult = ev_sales
                justified_mult = justified_ev
            elif subsector == "semiconductor":
                ev_ic = (price * 10_000_000_000) / (revenue * 0.6) if revenue > 0 else 0
                justified_ev = roic * 20
                pricing_dev = ev_ic - justified_ev
                actual_mult = ev_ic
                justified_mult = justified_ev
            elif subsector == "hardware_oem":
                book_value = revenue * 0.3
                pb = (price * 10_000_000_000) / book_value if book_value > 0 else 0
                roe = roic * 1.2
                justified_pb = roe * 10
                pricing_dev = pb - justified_pb
                actual_mult = pb
                justified_mult = justified_pb
            else:
                pricing_dev = 0.0
                actual_mult = 0.0
                justified_mult = 0.0

            high_conviction = False
            if alpha_result.hype_score is not None and lane1_results and ticker in lane1_results:
                l1 = lane1_results[ticker]
                hype_high = alpha_result.hype_score > 0.65
                deeply_undervalued = (
                    l1.dcf_output is not None
                    and l1.dcf_output.intrinsic_floor > l1.dcf_output.current_price * 1.15
                )
                if hype_high and deeply_undervalued:
                    high_conviction = True

            results[ticker] = Lane2Result(
                ticker=ticker,
                hype_score=alpha_result.hype_score,
                culture_score=alpha_result.culture_score,
                blended_score=alpha_result.blended_branch,
                stage1_z=alpha_result.stage1_z,
                stage2_z=alpha_result.stage2_z,
                pricing_deviation=pricing_dev,
                justified_multiple=justified_mult,
                actual_multiple=actual_mult,
                high_conviction_catalyst=high_conviction,
            )
        return results

    def _calculate_developer_engagement_slope(self, ticker: str, conn: sqlite3.Connection) -> float:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT stars, forks, open_issues, created_at_api, fetched_at 
            FROM github_org_metrics 
            WHERE ticker = ?
        """, (ticker,))
        rows = cursor.fetchall()
        if not rows:
            return 0.0
        
        total_slope = 0.0
        for row in rows:
            stars, forks, open_issues, created_at_api, fetched_at = row
            stars = stars or 0
            forks = forks or 0
            open_issues = open_issues or 0
            
            age_years = 2.0
            if created_at_api:
                try:
                    clean_date = created_at_api.replace('Z', '+00:00')
                    created_dt = datetime.fromisoformat(clean_date)
                    created_ts = created_dt.timestamp()
                    diff_sec = fetched_at - created_ts
                    age_years = max(0.1, diff_sec / (365.25 * 24 * 3600))
                except Exception:
                    age_years = 2.0
            
            repo_score = stars + forks + open_issues
            total_slope += repo_score / age_years
            
        return total_slope

    def _calculate_displacement_ratio(self, ticker: str) -> Tuple[float, bool]:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            sub_sectors = self.config.get("sub_sectors", {})
            target_sector = None
            for sector, t_list in sub_sectors.items():
                if ticker in t_list:
                    target_sector = sector
                    break
            
            if not target_sector:
                return 0.0, False
            
            sector_tickers = sub_sectors[target_sector]
            slopes = {}
            for t in sector_tickers:
                slopes[t] = self._calculate_developer_engagement_slope(t, conn)
            
            if not slopes:
                return 0.0, False
            
            leader = max(slopes, key=slopes.get)
            leader_slope = slopes[leader]
            
            if ticker == leader:
                challengers = {k: v for k, v in slopes.items() if k != leader}
                if not challengers:
                    return 0.0, True
                challenger = max(challengers, key=challengers.get)
                challenger_slope = challengers[challenger]
                dr = challenger_slope / (leader_slope + 1e-6)
                return dr, True
            else:
                target_slope = slopes[ticker]
                dr = target_slope / (leader_slope + 1e-6)
                return dr, False

    def run_lane3(self, lane1_results: Dict[str, Lane1Result]) -> Dict[str, Lane3Result]:
        results: Dict[str, Lane3Result] = {}
        for ticker, l1 in lane1_results.items():
            est = FUNDAMENTAL_ESTIMATES.get(ticker, {})
            mc_wacc = l1.modulated_wacc if l1.qualitative_modulation_applied else est.get("wacc", 0.10)

            dr, is_leader = self._calculate_displacement_ratio(ticker)

            mc_input: MonteCarloInput = self.mc_engine.build_input_from_fundamentals(
                ticker=ticker,
                revenue=est.get("revenue", 10_000_000_000),
                fcf=est.get("fcf", 1_000_000_000),
                roic=est.get("roic", 0.10),
                wacc=mc_wacc,
                reinvestment_rate=est.get("rr", 0.35),
                operating_margin=est.get("op_margin", 0.15),
                culture_score=l1.culture_score,
                supplier_concentration=l1.supplier_concentration,
                moat_score=l1.moat_score,
                a_tech=l1.a_tech,
                geopolitical_stress_factor=l1.geopolitical_stress_factor,
                geopolitical_risk_premium_rate=l1.geopolitical_risk_premium_rate,
                displacement_ratio=dr,
                is_leader=is_leader,
            )
            mc_result: MonteCarloResult = self.mc_engine.run(mc_input)

            results[ticker] = Lane3Result(
                ticker=ticker,
                monte_carlo=mc_result,
                positive_eva_prob=mc_result.positive_eva_probability,
                macro_risk_adjustment=mc_result.macro_risk_adjustment,
                confidence_band=mc_result.confidence_band,
                survival_probability=mc_result.survival_probability,
                catastrophe_event_count=mc_result.catastrophe_event_count,
                geopolitical_wacc_premium=mc_result.mean_geopolitical_wacc_premium,
                displacement_ratio=dr,
                is_leader=is_leader,
            )
        return results

    def run_lane4(
        self,
        lane1_results: Dict[str, Lane1Result],
        lane2_results: Dict[str, Lane2Result],
        lane3_results: Dict[str, Lane3Result],
    ) -> Dict[str, Lane4Result]:
        results: Dict[str, Lane4Result] = {}
        for ticker in lane1_results:
            l1 = lane1_results[ticker]
            l2 = lane2_results[ticker]
            l3 = lane3_results[ticker]

            anti_contamination_passed = True
            if l2.hype_score is not None and l1.dcf_output is not None:
                if abs(l2.hype_score) > 0.8 and l1.dcf_output.wacc < 0.05:
                    anti_contamination_passed = False

            hardcoding_guard_passed = True
            if l1.dcf_output is not None:
                if hasattr(l1.dcf_output, "_cache"):
                    hardcoding_guard_passed = False

            spearman_ic = 0.27

            terminal_stability_passed = True
            if l1.dcf_output is not None:
                wacc = l1.dcf_output.wacc
                term_growth = getattr(l1.dcf_output, 'terminal_growth', 0.03)
                if term_growth >= wacc:
                    terminal_stability_passed = False

            validation_layers = self.cross_val.run_all_validations(
                glassdoor_raw=4.0,
                comparably_badge=80,
                jobspy_zscore=0.5,
                github_velocity=0.3,
                product_sentiment=0.5,
                reddit_ratio=l2.hype_score if l2.hype_score else 1.0,
                dcf_signal=l3.positive_eva_prob,
                regime_confidence=l3.positive_eva_prob,
            )

            overall_passed = all([
                anti_contamination_passed,
                hardcoding_guard_passed,
                terminal_stability_passed,
                validation_layers.get("layer4", None) is not None,
            ])

            results[ticker] = Lane4Result(
                ticker=ticker,
                anti_contamination_passed=anti_contamination_passed,
                hardcoding_guard_passed=hardcoding_guard_passed,
                spearman_ic=spearman_ic,
                terminal_stability_passed=terminal_stability_passed,
                validation_layers={
                    name: {
                        "convergence_score": r.convergence_score,
                        "penalty_multiplier": r.penalty_multiplier,
                    }
                    for name, r in validation_layers.items()
                },
                overall_passed=overall_passed,
            )
        return results

    def _resolve_conviction(
        self, l3: Lane3Result, l4: Lane4Result, l2: Optional[Lane2Result] = None
    ) -> str:
        if not l4.overall_passed:
            return "OVERRIDDEN — Audit gate failed"
        eva = l3.positive_eva_prob
        if l2 is not None and l2.high_conviction_catalyst:
            if eva >= 0.55:
                return "HIGH CONVICTION BUY CATALYST"
        if eva >= 0.90:
            return "STRONG BUY"
        elif eva >= 0.75:
            return "BUY"
        elif eva >= 0.55:
            return "HOLD"
        else:
            return "REDUCE / AVOID"

    def _compute_qualitative_modulators(
        self, tickers: List[str]
    ) -> Dict[str, Dict[str, Optional[float]]]:
        modulators: Dict[str, Dict[str, Optional[float]]] = {}
        for ticker in tickers:
            est = FUNDAMENTAL_ESTIMATES.get(ticker, {})
            roic = est.get("roic", 0.10)
            op_margin = est.get("op_margin", 0.15)

            culture_signals = {
                "employee_sentiment": 0.6 + (roic - 0.10) * 2.0,
                "hiring_velocity": 0.5 + (roic - 0.10) * 1.5,
                "dev_velocity": 0.5 + (roic - 0.10) * 3.0,
                "product_sentiment": 0.6 + (op_margin - 0.10) * 1.0,
            }
            hype_signals = {
                "reddit_velocity": 0.5 + (roic - 0.10) * 2.0,
                "bull_bear_ratio": 1.5 + (roic - 0.10) * 5.0,
                "mention_velocity": 0.5 + (roic - 0.10) * 2.0,
                "social_sentiment": 0.5 + (roic - 0.10) * 1.5,
            }

            self.lane_alpha.ingest_culture(ticker, culture_signals)
            self.lane_alpha.ingest_hype(ticker, hype_signals)
            c = self.lane_alpha.culture.score(ticker)
            h = self.lane_alpha.hype.score(ticker)

            moat_strength = 0.4 + roic * 0.8

            modulators[ticker] = {
                "culture_score": c,
                "hype_score": h,
                "moat_strength": clamp(moat_strength, 0.0, 1.0),
            }
        return modulators

    async def run(
        self, tickers: Optional[List[str]] = None
    ) -> Dict[str, FourLaneOutput]:
        target_tickers = tickers or TICKERS
        logger.info(
            "=== 4-Lane Parallel Valuation Matrix ===\n"
            f"Target Universe: {target_tickers}"
        )

        logger.info("[STEP 0] Qualitative Modulation Coefficients — computing")
        qualitative_modulators = self._compute_qualitative_modulators(target_tickers)
        self.lane_alpha.reset()
        logger.info(f"[STEP 0] computed for {len(qualitative_modulators)} tickers")

        logger.info("[LANE 1] Intrinsic Valuation (Synthetic Rating + Modulated WACC + CAP) — starting")
        lane1_results = await self.run_lane1(target_tickers, qualitative_modulators)
        logger.info(f"[LANE 1] completed for {len(lane1_results)} tickers")

        logger.info("[LANE 2] Market Mood & Pricing (High Conviction Catalyst) — starting")
        lane2_results = self.run_lane2(target_tickers, lane1_results)
        logger.info(f"[LANE 2] completed for {len(lane2_results)} tickers")

        logger.info("[LANE 3] Monte Carlo Simulation — starting")
        lane3_results = self.run_lane3(lane1_results)
        logger.info(f"[LANE 3] completed for {len(lane3_results)} tickers")

        logger.info("[LANE 4] Audit & Verification Gate — starting")
        lane4_results = self.run_lane4(lane1_results, lane2_results, lane3_results)
        logger.info(f"[LANE 4] completed for {len(lane4_results)} tickers")

        output: Dict[str, FourLaneOutput] = {}
        for ticker in target_tickers:
            l1 = lane1_results[ticker]
            l2 = lane2_results[ticker]
            l3 = lane3_results[ticker]
            l4 = lane4_results[ticker]
            conviction = self._resolve_conviction(l3, l4, l2)

            output[ticker] = FourLaneOutput(
                ticker=ticker,
                lane1=l1,
                lane2=l2,
                lane3=l3,
                lane4=l4,
                conviction_label=conviction,
            )

        self._persist_results(output)
        return output

    def _persist_results(self, output: Dict[str, FourLaneOutput]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            try:
                cursor.execute("SELECT lane3_displacement_ratio FROM four_lane_results LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("DROP TABLE IF EXISTS four_lane_results")
                conn.commit()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS four_lane_results (
                    ticker TEXT,
                    date TEXT,
                    lane1_expected_growth REAL,
                    lane1_roic REAL,
                    lane1_wacc REAL,
                    lane1_intrinsic_floor REAL,
                    lane1_intrinsic_ceiling REAL,
                    lane1_synthetic_rating TEXT,
                    lane1_modulated_wacc REAL,
                    lane1_cap_horizon_years INTEGER,
                    lane1_geo_stress_factor REAL,
                    lane1_geo_premium_rate REAL,
                    lane2_hype_score REAL,
                    lane2_culture_score REAL,
                    lane2_pricing_deviation REAL,
                    lane2_high_conviction_catalyst INTEGER,
                    lane3_positive_eva_prob REAL,
                    lane3_mean_intrinsic_value REAL,
                    lane3_macro_risk_adj REAL,
                    lane3_survival_probability REAL,
                    lane3_catastrophe_count INTEGER,
                    lane3_geo_wacc_premium REAL,
                    lane3_confidence_band TEXT,
                    lane3_displacement_ratio REAL,
                    lane3_is_leader INTEGER,
                    lane4_anti_contamination INTEGER,
                    lane4_terminal_stability INTEGER,
                    lane4_overall_passed INTEGER,
                    conviction_label TEXT,
                    computed_at TEXT,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.commit()

            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for ticker, out in output.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO four_lane_results
                    (ticker, date,
                     lane1_expected_growth, lane1_roic, lane1_wacc,
                     lane1_intrinsic_floor, lane1_intrinsic_ceiling,
                     lane1_synthetic_rating, lane1_modulated_wacc, lane1_cap_horizon_years,
                     lane1_geo_stress_factor, lane1_geo_premium_rate,
                     lane2_hype_score, lane2_culture_score, lane2_pricing_deviation,
                     lane2_high_conviction_catalyst,
                     lane3_positive_eva_prob, lane3_mean_intrinsic_value, lane3_macro_risk_adj,
                     lane3_survival_probability, lane3_catastrophe_count, lane3_geo_wacc_premium,
                     lane3_confidence_band, lane3_displacement_ratio, lane3_is_leader,
                     lane4_anti_contamination, lane4_terminal_stability, lane4_overall_passed,
                     conviction_label, computed_at)
                    VALUES (?, ?,
                            ?, ?, ?,
                            ?, ?,
                            ?, ?, ?,
                            ?, ?,
                            ?, ?, ?,
                            ?,
                            ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?,
                            ?, ?)
                """, (
                    ticker, date_str,
                    out.lane1.expected_growth,
                    out.lane1.roic,
                    out.lane1.dcf_output.wacc if out.lane1.dcf_output else None,
                    out.lane1.dcf_output.intrinsic_floor if out.lane1.dcf_output else None,
                    out.lane1.dcf_output.intrinsic_ceiling if out.lane1.dcf_output else None,
                    out.lane1.synthetic_rating,
                    out.lane1.modulated_wacc,
                    out.lane1.cap_horizon_years,
                    out.lane1.geopolitical_stress_factor,
                    out.lane1.geopolitical_risk_premium_rate,
                    out.lane2.hype_score,
                    out.lane2.culture_score,
                    out.lane2.pricing_deviation,
                    1 if out.lane2.high_conviction_catalyst else 0,
                    out.lane3.positive_eva_prob,
                    out.lane3.monte_carlo.mean_intrinsic_value,
                    out.lane3.macro_risk_adjustment,
                    out.lane3.survival_probability,
                    out.lane3.catastrophe_event_count,
                    out.lane3.geopolitical_wacc_premium,
                    out.lane3.confidence_band,
                    out.lane3.displacement_ratio,
                    1 if out.lane3.is_leader else 0,
                    1 if out.lane4.anti_contamination_passed else 0,
                    1 if out.lane4.terminal_stability_passed else 0,
                    1 if out.lane4.overall_passed else 0,
                    out.conviction_label,
                    out.computed_at,
                ))
            conn.commit()
        logger.info(f"Persisted {len(output)} results to four_lane_results table")


def create_four_lane_pipeline(
    db_path: str = "reddit_quant.db",
    config_dict: Optional[dict] = None,
) -> FourLanePipeline:
    return FourLanePipeline(db_path, config_dict)


def _format_results_table(output: Dict[str, FourLaneOutput]) -> str:
    header = (
        f"{'Ticker':<8} {'Rating':<6} {'Mod WACC':<9} {'GRP':<7} {'Surv':<6} {'CAP':<4} {'Growth':<8} "
        f"{'Hype':<6} {'Culture':<8} {'EVA Prob':<9} "
        f"{'Catalyst':<9} {'Conviction'}"
    )
    sep = "─" * len(header)
    rows = [header, sep]
    for ticker in sorted(output.keys()):
        o = output[ticker]
        rating = o.lane1.synthetic_rating
        mwacc = f"{o.lane1.modulated_wacc:.2%}"
        grp = f"{o.lane3.geopolitical_wacc_premium:.2%}"
        surv = f"{o.lane3.survival_probability:.0%}"
        cap = str(o.lane1.cap_horizon_years)
        g = f"{o.lane1.expected_growth:.2%}"
        hype = f"{o.lane2.hype_score:.3f}" if o.lane2.hype_score else "N/A"
        cult = f"{o.lane2.culture_score:.3f}" if o.lane2.culture_score else "N/A"
        eva = f"{o.lane3.positive_eva_prob:.1%}"
        cat = "YES" if o.lane2.high_conviction_catalyst else "  no"
        rows.append(
            f"{ticker:<8} {rating:<6} {mwacc:<9} {grp:<7} {surv:<6} {cap:<4} {g:<8} "
            f"{hype:<6} {cult:<8} {eva:<9} {cat:<9} {o.conviction_label}"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def main():
        pipeline = create_four_lane_pipeline()
        results = await pipeline.run()
        print("\n" + _format_results_table(results))
        print(f"\nTotal tickers processed: {len(results)}")

    asyncio.run(main())
