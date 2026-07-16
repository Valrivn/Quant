from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from Quantitative.fragility.segment_data import (
    COMPANY_SEGMENTS,
    COMPANY_FUZZY_WEIGHTS,
    COMPANY_FINANCIALS,
)
from Quantitative.fragility.sector_fragility import SectorFragilityTable
from Quantitative.fragility.power_law_overlay import PowerLawOverlay, FragilityOutput
from Quantitative.dividends.company_risk_overlay import (
    CompanyRiskOverlay,
    RiskOverlayOutput,
)
from Quantitative.funds.asset_type_detector import AssetTypeDetector, AssetType
from Quantitative.sensitivity.sensitivity_vector import SensitivityEngine, SensitivityVector

logger = logging.getLogger(__name__)

YFINANCE_SECTOR_TO_TAG: Dict[str, List[Tuple[str, float]]] = {
    "Technology": [("TECHNOLOGY", 1.0)],
    "Communication Services": [("TELECOMMUNICATIONS", 0.6), ("TECHNOLOGY", 0.4)],
    "Consumer Cyclical": [("CONSUMER_CYCLICAL", 1.0)],
    "Consumer Defensive": [("CONSUMER_DEFENSIVE", 1.0)],
    "Healthcare": [("HEALTHCARE", 1.0)],
    "Financial Services": [("FINANCIAL_SERVICES", 1.0)],
    "Industrials": [("INDUSTRIAL", 1.0)],
    "Utilities": [("UTILITIES", 1.0)],
    "Real Estate": [("REAL_ESTATE", 1.0)],
    "Energy": [("CYCLICAL_IND", 1.0)],
    "Basic Materials": [("CYCLICAL_IND", 1.0)],
}

YFINANCE_INDUSTRY_OVERRIDES: Dict[str, List[Tuple[str, float]]] = {
    "Semiconductors": [("SEMICONDUCTOR", 1.0)],
    "Software - Infrastructure": [("PLATFORM_SOFTWARE", 0.8), ("TECHNOLOGY", 0.2)],
    "Software - Application": [("PLATFORM_SOFTWARE", 0.9), ("TECHNOLOGY", 0.1)],
    "Internet Content & Information": [("PLATFORM_SOFTWARE", 0.7), ("TECHNOLOGY", 0.3)],
    "Internet Retail": [("CONSUMER_CYCLICAL", 0.6), ("TECHNOLOGY", 0.4)],
    "Consumer Electronics": [("HARDWARE_OEM", 0.7), ("TECHNOLOGY", 0.3)],
    "Computer Hardware": [("HARDWARE_OEM", 0.8), ("TECHNOLOGY", 0.2)],
    "Electronic Components": [("HARDWARE_OEM", 0.6), ("TECHNOLOGY", 0.4)],
    "Auto Manufacturers": [("AUTOMOTIVE", 0.8), ("CONSUMER_CYCLICAL", 0.2)],
    "Banks - Diversified": [("FINANCIAL_SERVICES", 1.0)],
    "Asset Management": [("FINANCIAL_SERVICES", 0.8), ("REAL_ESTATE", 0.2)],
    "Biotechnology": [("HEALTHCARE", 0.8), ("TECHNOLOGY", 0.2)],
    "Drug Manufacturers - General": [("HEALTHCARE", 1.0)],
    "Drug Manufacturers - Specialty": [("HEALTHCARE", 1.0)],
    "Medical Devices": [("HEALTHCARE", 0.7), ("INDUSTRIAL", 0.3)],
    "Telecom Services": [("TELECOMMUNICATIONS", 1.0)],
    "REITs": [("REITS", 1.0)],
    "Real Estate - Development": [("REAL_ESTATE", 1.0)],
    "Real Estate - Diversified": [("REAL_ESTATE", 1.0)],
    "Oil & Gas Integrated": [("CYCLICAL_IND", 0.7), ("INDUSTRIAL", 0.3)],
    "Oil & Gas E&P": [("CYCLICAL_IND", 0.8), ("INDUSTRIAL", 0.2)],
    "Chemicals": [("CYCLICAL_IND", 0.7), ("INDUSTRIAL", 0.3)],
    "Steel": [("CYCLICAL_IND", 0.8), ("INDUSTRIAL", 0.2)],
    "Aerospace & Defense": [("INDUSTRIAL", 0.7), ("CYCLICAL_IND", 0.3)],
    "Electrical Equipment": [("INDUSTRIAL", 0.6), ("HARDWARE_OEM", 0.4)],
    "Insurance - Diversified": [("FINANCIAL_SERVICES", 1.0)],
    "Insurance - Property & Casualty": [("FINANCIAL_SERVICES", 1.0)],
    "Beverages - Non-Alcoholic": [("CONSUMER_DEFENSIVE", 1.0)],
    "Beverages - Alcoholic": [("CONSUMER_DEFENSIVE", 1.0)],
    "Packaged Foods": [("CONSUMER_DEFENSIVE", 1.0)],
    "Retail - Defensive": [("CONSUMER_DEFENSIVE", 1.0)],
}

DEFAULT_FUZZY_WEIGHTS_PER_TAG: Dict[str, Dict[str, float]] = {
    "PLATFORM_SOFTWARE": {"FAST_GROWER": 0.50, "STALWART": 0.30, "CYCLICAL": 0.15, "SLOW_GROWER": 0.05},
    "TECHNOLOGY": {"FAST_GROWER": 0.40, "STALWART": 0.30, "CYCLICAL": 0.20, "SLOW_GROWER": 0.10},
    "SEMICONDUCTOR": {"FAST_GROWER": 0.35, "CYCLICAL": 0.35, "STALWART": 0.20, "TURNAROUND": 0.10},
    "HARDWARE_OEM": {"CYCLICAL": 0.45, "STALWART": 0.30, "FAST_GROWER": 0.15, "TURNAROUND": 0.10},
    "CONSUMER_CYCLICAL": {"CYCLICAL": 0.50, "STALWART": 0.25, "TURNAROUND": 0.15, "FAST_GROWER": 0.10},
    "AUTOMOTIVE": {"CYCLICAL": 0.60, "TURNAROUND": 0.20, "STALWART": 0.15, "SLOW_GROWER": 0.05},
    "FINANCIAL_SERVICES": {"STALWART": 0.45, "SLOW_GROWER": 0.25, "CYCLICAL": 0.20, "FAST_GROWER": 0.10},
    "HEALTHCARE": {"STALWART": 0.50, "FAST_GROWER": 0.25, "SLOW_GROWER": 0.15, "CYCLICAL": 0.10},
    "CONSUMER_DEFENSIVE": {"STALWART": 0.50, "SLOW_GROWER": 0.30, "CYCLICAL": 0.10, "FAST_GROWER": 0.10},
    "INDUSTRIAL": {"CYCLICAL": 0.40, "STALWART": 0.30, "TURNAROUND": 0.15, "FAST_GROWER": 0.15},
    "CYCLICAL_IND": {"CYCLICAL": 0.50, "TURNAROUND": 0.20, "STALWART": 0.20, "ASSET_PLAY": 0.10},
    "UTILITIES": {"SLOW_GROWER": 0.50, "STALWART": 0.30, "CYCLICAL": 0.10, "TURNAROUND": 0.10},
    "REAL_ESTATE": {"SLOW_GROWER": 0.40, "STALWART": 0.30, "CYCLICAL": 0.20, "ASSET_PLAY": 0.10},
    "REITS": {"SLOW_GROWER": 0.45, "STALWART": 0.30, "CYCLICAL": 0.15, "ASSET_PLAY": 0.10},
    "NETWORKS": {"FAST_GROWER": 0.40, "STALWART": 0.30, "CYCLICAL": 0.20, "TECHNOLOGY": 0.10},
    "TELECOMMUNICATIONS": {"SLOW_GROWER": 0.40, "STALWART": 0.30, "CYCLICAL": 0.20, "FAST_GROWER": 0.10},
}

YFINANCE_SECTOR_WEIGHTING_TO_TAG: Dict[str, List[Tuple[str, float]]] = {
    "technology": [("TECHNOLOGY", 0.7), ("PLATFORM_SOFTWARE", 0.3)],
    "communication_services": [("PLATFORM_SOFTWARE", 0.7), ("TELECOMMUNICATIONS", 0.3)],
    "consumer_cyclical": [("CONSUMER_CYCLICAL", 1.0)],
    "healthcare": [("HEALTHCARE", 1.0)],
    "consumer_defensive": [("CONSUMER_DEFENSIVE", 1.0)],
    "financial_services": [("FINANCIAL_SERVICES", 1.0)],
    "industrials": [("INDUSTRIAL", 0.6), ("CYCLICAL_IND", 0.4)],
    "basic_materials": [("CYCLICAL_IND", 1.0)],
    "utilities": [("UTILITIES", 1.0)],
    "realestate": [("REAL_ESTATE", 0.6), ("REITS", 0.4)],
    "energy": [("CYCLICAL_IND", 1.0)],
}


def _normalize_weights(d: Dict[str, float]) -> Dict[str, float]:
    total = sum(d.values())
    if total <= 0:
        return d
    return {k: v / total for k, v in d.items()}


@dataclass
class HoldingData:
    ticker: str
    weight: float
    segments: List[Tuple[str, float]]
    fuzzy_weights: Dict[str, float]
    total_debt: float
    tangible_assets: float
    tax_rate: float
    interest_coverage_ratio: float
    data_source: str


@dataclass
class IndexFundResult:
    ticker: str
    asset_type: AssetType
    holdings_df: pd.DataFrame
    hhi: float
    n_holdings: int
    coverage_weight: float
    coverage_count: int
    aggregated_segments: List[Tuple[str, float]]
    aggregated_fuzzy_weights: Dict[str, float]
    aggregated_total_debt: float
    aggregated_tangible_assets: float
    aggregated_tax_rate: float
    holding_details: List[HoldingData]
    aggregated_icr: float = 0.0
    sector_weightings_raw: Optional[Dict[str, float]] = None
    derived_from_sectors: bool = False
    fragility_output: Optional[FragilityOutput] = None
    risk_output: Optional[RiskOverlayOutput] = None
    sensitivity_vector: Optional[SensitivityVector] = None


def _map_yfinance_to_segments(sector: str, industry: str) -> List[Tuple[str, float]]:
    industry_key = industry.strip() if industry else ""
    if industry_key in YFINANCE_INDUSTRY_OVERRIDES:
        return YFINANCE_INDUSTRY_OVERRIDES[industry_key]
    sector_key = sector.strip() if sector else ""
    if sector_key in YFINANCE_SECTOR_TO_TAG:
        return YFINANCE_SECTOR_TO_TAG[sector_key]
    return [("TECHNOLOGY", 1.0)]


def _lookup_fuzzy_weights(ticker: str, segments: List[Tuple[str, float]]) -> Dict[str, float]:
    hardcoded = COMPANY_FUZZY_WEIGHTS.get(ticker)
    if hardcoded is not None:
        return dict(hardcoded)
    w: Dict[str, float] = {}
    for tag, seg_weight in segments:
        defaults = DEFAULT_FUZZY_WEIGHTS_PER_TAG.get(tag, {})
        for cat, val in defaults.items():
            w[cat] = w.get(cat, 0.0) + seg_weight * val
    return _normalize_weights(w)


def _fetch_yfinance_financials(ticker: str) -> Tuple[float, float, float, float]:
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        total_debt = float(info.get("totalDebt") or 0)
        bs = t.balance_sheet
        tangible_assets = 0.0
        if bs is not None and not bs.empty:
            col = bs.columns[0]
            for idx in bs.index:
                if "tangible" in str(idx).lower() and "book" in str(idx).lower():
                    val = bs.loc[idx, col]
                    if pd.notna(val) and val is not None:
                        tangible_assets = float(val)
                        break
            if tangible_assets <= 0:
                for idx in bs.index:
                    if "equity" in str(idx).lower() and "gross" not in str(idx).lower() and "minority" not in str(idx).lower():
                        val = bs.loc[idx, col]
                        if pd.notna(val) and val is not None:
                            tangible_assets = float(val)
                            break
        tax_rate = float(info.get("taxRateForCalcs") or 0.21)
        if tax_rate <= 0 or tax_rate >= 0.50:
            tax_rate = 0.21

        # Interest Coverage Ratio from yfinance info
        icr = float(info.get("interestCoverage") or 0.0)

        return total_debt, tangible_assets, tax_rate, icr
    except Exception as e:
        logger.warning(f"Failed to fetch financials for {ticker}: {e}")
        return 0.0, 0.0, 0.21, 0.0


def _fetch_holding_segments(ticker: str) -> List[Tuple[str, float]]:
    hardcoded = COMPANY_SEGMENTS.get(ticker)
    if hardcoded is not None:
        return [list(t) for t in hardcoded]
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        sector = info.get("sector", "") or ""
        industry = info.get("industry", "") or ""
        return _map_yfinance_to_segments(sector, industry)
    except Exception as e:
        logger.warning(f"Failed to fetch segments for {ticker}: {e}")
        return [("TECHNOLOGY", 1.0)]


def _merge_segments(
    segments_list: List[List[Tuple[str, float]]], weights: List[float],
) -> List[Tuple[str, float]]:
    merged: Dict[str, float] = {}
    total_w = sum(abs(w) for w in weights)
    if total_w <= 0:
        return []
    norm_weights = [w / total_w for w in weights]
    for segs, nw in zip(segments_list, norm_weights):
        for tag, seg_w in segs:
            merged[tag] = merged.get(tag, 0.0) + nw * seg_w
    total = sum(merged.values())
    if total <= 0:
        return []
    return [(tag, w / total) for tag, w in merged.items()]


def _merge_fuzzy_weights(
    fw_list: List[Dict[str, float]], weights: List[float],
) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    total_w = sum(abs(w) for w in weights)
    if total_w <= 0:
        return {}
    norm_weights = [w / total_w for w in weights]
    for fw, nw in zip(fw_list, norm_weights):
        for cat, val in fw.items():
            merged[cat] = merged.get(cat, 0.0) + nw * val
    return _normalize_weights(merged)


def _weighted_avg(values: List[float], weights: List[float]) -> float:
    total_w = sum(abs(w) for w in weights)
    if total_w <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_w


class IndexFundHandler:
    def __init__(
        self,
        fragility_overlay: Optional[PowerLawOverlay] = None,
        risk_overlay: Optional[CompanyRiskOverlay] = None,
        sensitivity_engine: Optional[SensitivityEngine] = None,
        max_holdings: int = 15,
    ):
        self.fragility = fragility_overlay or PowerLawOverlay()
        self.risk = risk_overlay or CompanyRiskOverlay(omega=0.0)
        self.sensitivity = sensitivity_engine or SensitivityEngine()
        self.max_holdings = max_holdings

    def fetch_holdings(self, ticker: str) -> pd.DataFrame:
        import yfinance as yf
        t = yf.Ticker(ticker)
        holdings = t.funds_data.top_holdings
        if holdings is None or holdings.empty:
            raise ValueError(f"No holdings data for {ticker}")
        holdings = holdings.head(self.max_holdings)
        holdings["Holding Percent"] = pd.to_numeric(
            holdings["Holding Percent"], errors="coerce"
        ).fillna(0.0)
        return holdings

    @staticmethod
    def compute_hhi(holdings: pd.DataFrame) -> float:
        weights = holdings["Holding Percent"].values
        return float(sum(w ** 2 for w in weights))

    def resolve_holding(self, ticker: str, weight: float) -> HoldingData:
        hardcoded_segments = COMPANY_SEGMENTS.get(ticker)
        hardcoded_fuzzy = COMPANY_FUZZY_WEIGHTS.get(ticker)
        hardcoded_financials = COMPANY_FINANCIALS.get(ticker)

        if hardcoded_segments is not None:
            segments = [list(t) for t in hardcoded_segments]
            fuzzy = dict(hardcoded_fuzzy) if hardcoded_fuzzy else _lookup_fuzzy_weights(ticker, segments)
            fin = hardcoded_financials
            if fin:
                total_debt = fin["total_debt"]
                tangible_assets = fin["tangible_assets"]
                tax_rate = fin["tax_rate"]
                icr = 0.0  # hardcoded financials don't include ICR
            else:
                total_debt, tangible_assets, tax_rate, icr = _fetch_yfinance_financials(ticker)
            return HoldingData(
                ticker=ticker, weight=weight,
                segments=segments, fuzzy_weights=fuzzy,
                total_debt=total_debt, tangible_assets=tangible_assets,
                tax_rate=tax_rate, interest_coverage_ratio=icr,
                data_source="hardcoded",
            )
        segments = _fetch_holding_segments(ticker)
        fuzzy = _lookup_fuzzy_weights(ticker, segments)
        total_debt, tangible_assets, tax_rate, icr = _fetch_yfinance_financials(ticker)
        return HoldingData(
            ticker=ticker, weight=weight,
            segments=segments, fuzzy_weights=fuzzy,
            total_debt=total_debt, tangible_assets=tangible_assets,
            tax_rate=tax_rate, interest_coverage_ratio=icr,
            data_source="yfinance" if total_debt > 0 or tangible_assets > 0 else "defaults",
        )

    def aggregate(self, ticker: str) -> IndexFundResult:
        import yfinance as yf
        asset_type = AssetTypeDetector.detect(ticker)
        if asset_type not in (AssetType.ETF, AssetType.MUTUAL_FUND):
            raise ValueError(
                f"{ticker} is {asset_type.value}, not a fund. "
                "Use CompanyClassifier for equities."
            )
        holdings = self.fetch_holdings(ticker)
        weights = holdings["Holding Percent"].tolist()
        raw_tickers = [str(idx).strip() for idx in holdings.index]
        hhi = self.compute_hhi(holdings)

        details: List[HoldingData] = []
        covered_weight = 0.0
        covered_count = 0
        all_segments_lists: List[List[Tuple[str, float]]] = []
        all_fuzzy_lists: List[Dict[str, float]] = []
        debt_list: List[float] = []
        tangible_list: List[float] = []
        tax_list: List[float] = []
        icr_list: List[float] = []
        detail_weights: List[float] = []

        for h_ticker, w in zip(raw_tickers, weights):
            hd = self.resolve_holding(h_ticker, w)
            details.append(hd)

            has_data = hd.data_source in ("hardcoded", "yfinance") and (
                len(hd.segments) > 0 or sum(hd.fuzzy_weights.values()) > 0
            )
            if has_data:
                covered_count += 1
                covered_weight += w

            all_segments_lists.append(hd.segments)
            all_fuzzy_lists.append(hd.fuzzy_weights)
            debt_list.append(hd.total_debt)
            tangible_list.append(hd.tangible_assets)
            tax_list.append(hd.tax_rate)
            icr_list.append(hd.interest_coverage_ratio)
            detail_weights.append(w)

        agg_segments = _merge_segments(all_segments_lists, detail_weights)
        agg_fuzzy = _merge_fuzzy_weights(all_fuzzy_lists, detail_weights)
        agg_debt = _weighted_avg(debt_list, detail_weights)
        agg_tangible = _weighted_avg(tangible_list, detail_weights)
        agg_tax = _weighted_avg(tax_list, detail_weights)
        agg_icr = _weighted_avg(icr_list, detail_weights)

        return IndexFundResult(
            ticker=ticker,
            asset_type=asset_type,
            holdings_df=holdings,
            hhi=hhi,
            n_holdings=len(raw_tickers),
            coverage_weight=covered_weight,
            coverage_count=covered_count,
            aggregated_segments=agg_segments,
            aggregated_fuzzy_weights=agg_fuzzy,
            aggregated_total_debt=agg_debt,
            aggregated_tangible_assets=agg_tangible,
            aggregated_tax_rate=agg_tax,
            aggregated_icr=agg_icr,
            holding_details=details,
        )

    @staticmethod
    def fetch_sector_weightings(ticker: str) -> Optional[Dict[str, float]]:
        import yfinance as yf
        try:
            t = yf.Ticker(ticker)
            sw = t.funds_data.sector_weightings
            if sw and isinstance(sw, dict) and len(sw) > 0:
                return {k: float(v) for k, v in sw.items()}
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch sector weightings for {ticker}: {e}")
            return None

    @staticmethod
    def _sector_weightings_to_segments(
        sector_weightings: Dict[str, float],
    ) -> List[Tuple[str, float]]:
        merged: Dict[str, float] = {}
        total_w = sum(sector_weightings.values())
        if total_w <= 0:
            return [("TECHNOLOGY", 1.0)]
        for yf_sector, w in sector_weightings.items():
            norm_w = w / total_w
            mapping = YFINANCE_SECTOR_WEIGHTING_TO_TAG.get(yf_sector, [("TECHNOLOGY", 1.0)])
            for tag, tag_w in mapping:
                merged[tag] = merged.get(tag, 0.0) + norm_w * tag_w
        total = sum(merged.values())
        if total <= 0:
            return [("TECHNOLOGY", 1.0)]
        return [(tag, w / total) for tag, w in merged.items()]

    @staticmethod
    def _sector_weightings_to_fuzzy(
        segments: List[Tuple[str, float]],
    ) -> Dict[str, float]:
        w: Dict[str, float] = {}
        for tag, seg_w in segments:
            defaults = DEFAULT_FUZZY_WEIGHTS_PER_TAG.get(tag, {})
            for cat, val in defaults.items():
                w[cat] = w.get(cat, 0.0) + seg_w * val
        return _normalize_weights(w)

    def aggregate_from_sectors(self, ticker: str) -> IndexFundResult:
        asset_type = AssetTypeDetector.detect(ticker)
        if asset_type not in (AssetType.ETF, AssetType.MUTUAL_FUND):
            raise ValueError(
                f"{ticker} is {asset_type.value}, not a fund. "
                "Use CompanyClassifier for equities."
            )

        sector_weightings = self.fetch_sector_weightings(ticker)
        if not sector_weightings:
            raise ValueError(f"No sector weightings available for {ticker}")

        agg_segments = self._sector_weightings_to_segments(sector_weightings)
        agg_fuzzy = self._sector_weightings_to_fuzzy(agg_segments)

        try:
            holdings = self.fetch_holdings(ticker)
            weights = holdings["Holding Percent"].tolist()
            raw_tickers = [str(idx).strip() for idx in holdings.index]
            hhi = self.compute_hhi(holdings)

            details: List[HoldingData] = []
            debt_list: List[float] = []
            tangible_list: List[float] = []
            tax_list: List[float] = []
            icr_list: List[float] = []
            detail_weights: List[float] = []

            for h_ticker, w in zip(raw_tickers, weights):
                hd = self.resolve_holding(h_ticker, w)
                details.append(hd)
                debt_list.append(hd.total_debt)
                tangible_list.append(hd.tangible_assets)
                tax_list.append(hd.tax_rate)
                icr_list.append(hd.interest_coverage_ratio)
                detail_weights.append(w)

            agg_debt = _weighted_avg(debt_list, detail_weights)
            agg_tangible = _weighted_avg(tangible_list, detail_weights)
            agg_tax = _weighted_avg(tax_list, detail_weights)
            agg_icr = _weighted_avg(icr_list, detail_weights)
        except Exception as e:
            logger.warning(f"Could not fetch holdings for financials ({e}), using defaults")
            details = []
            agg_debt = 0.0
            agg_tangible = 0.0
            agg_tax = 0.21
            agg_icr = 0.0
            hhi = 0.0

        return IndexFundResult(
            ticker=ticker,
            asset_type=asset_type,
            holdings_df=pd.DataFrame(),
            hhi=hhi,
            n_holdings=len(details),
            coverage_weight=1.0,
            coverage_count=len(details),
            aggregated_segments=agg_segments,
            aggregated_fuzzy_weights=agg_fuzzy,
            aggregated_total_debt=agg_debt,
            aggregated_tangible_assets=agg_tangible,
            aggregated_tax_rate=agg_tax,
            aggregated_icr=agg_icr,
            holding_details=details,
            sector_weightings_raw=sector_weightings,
            derived_from_sectors=True,
        )

    @staticmethod
    def _diversification_dampener(
        theta_base: float,
        lifecycle_core: float,
        segments: List[Tuple[str, float]],
    ) -> Tuple[float, float]:
        seg_weights = [w for _, w in segments]
        seg_hhi = sum(w ** 2 for w in seg_weights)
        n_eff = 1.0 / seg_hhi if seg_hhi > 0 else 1.0
        damp = math.exp(-n_eff / 3.0)
        theta_damp = 1.0 + (theta_base - 1.0) * damp
        lc_damp = 1.0 + (lifecycle_core - 1.0) * damp
        return theta_damp, lc_damp

    def evaluate(
        self,
        ticker: str,
        use_sector_weightings: bool = True,
        inflation: float = 2.5,
    ) -> IndexFundResult:
        if use_sector_weightings:
            sw = self.fetch_sector_weightings(ticker)
            if sw:
                result = self.aggregate_from_sectors(ticker)
            else:
                logger.info(f"No sector weightings for {ticker}, falling back to top-holdings aggregation")
                result = self.aggregate(ticker)
        else:
            result = self.aggregate(ticker)

        theta_base, beta_portfolio, _ = (
            SectorFragilityTable.compute_sector_foundation(
                result.aggregated_segments
            )
        )

        lc_power = self.fragility.compute_lifecycle_core(
            result.aggregated_fuzzy_weights, use_power=True
        )

        if result.derived_from_sectors:
            theta_base, lc_power = self._diversification_dampener(
                theta_base, lc_power, result.aggregated_segments
            )

        lev_mult = PowerLawOverlay.compute_leverage_multiplier(
            result.aggregated_total_debt,
            result.aggregated_tangible_assets,
            result.aggregated_tax_rate,
        )

        phi_adjusted = theta_base * lc_power * lev_mult
        phi_arithmetic = theta_base * self.fragility.compute_lifecycle_core(
            result.aggregated_fuzzy_weights, use_power=False
        ) * lev_mult

        from Quantitative.fragility.power_law_overlay import FragilityOutput
        result.fragility_output = FragilityOutput(
            ticker=ticker,
            theta_base=theta_base,
            beta_portfolio=beta_portfolio,
            lifecycle_core_arithmetic=0.0,
            lifecycle_core_power=lc_power,
            power_exponent=self.fragility.p,
            leverage_multiplier=lev_mult,
            phi_adjusted=phi_adjusted,
            phi_arithmetic=phi_arithmetic,
        )

        risk_output = self.risk.compute_overlay(
            ticker=ticker,
            fuzzy_weights=result.aggregated_fuzzy_weights,
            dividend_metrics=None,
        )
        result.risk_output = risk_output

        # Compute Relative Sensitivity Vector
        # Use leverage_multiplier as ICR proxy if real ICR is unavailable
        icr_for_sensitivity = result.aggregated_icr
        if icr_for_sensitivity <= 0.0:
            # Fallback: derive ICR proxy from leverage ratio
            # Higher leverage -> lower effective ICR
            icr_for_sensitivity = max(0.5, 15.0 / max(lev_mult, 1.0))

        result.sensitivity_vector = self.sensitivity.compute(
            hhi=result.hhi,
            icr=icr_for_sensitivity,
            inflation=inflation,
            ticker=ticker,
        )

        return result
