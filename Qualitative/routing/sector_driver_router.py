"""Sector Driver Router for Multi-Sector Qualitative Alpha Pipeline.

Routes tickers to their primary sector-native traction metrics based on GICS sector:
1. Tech / Communication Services:
   GitHub (PRs/contributors) + Hugging Face + USPTO Patents
   T_i = 0.35 * Z_dev + 0.35 * Z_ai + 0.30 * Z_patent_quality
2. Healthcare / Biotech:
   ClinicalTrials.gov phase transitions + FDA patent exclusivity + USPTO Patents
   T_i = 0.50 * Z_clinical_trials + 0.30 * Z_fda_exclusivity + 0.20 * Z_patent_quality
3. Financials:
   CFPB consumer complaints + FDIC margins
   T_i = 0.50 * Z_cfpb_safety + 0.50 * Z_fdic_margins
4. Industrials / Defense:
   USASpending contract backlog + NHTSA recalls + USPTO Patents
   T_i = 0.50 * Z_usaspending_backlog + 0.30 * Z_patent_quality + 0.20 * Z_nhtsa_safety
5. Other / Consumer:
   Wikimedia pageview velocity + ASC 606 RPO backlog
   T_i = 0.50 * Z_wiki_mindshare + 0.50 * Z_rpo_backlog

Enforces strict Point-In-Time (PIT) timestamps: every record must carry an unalterable
timestamp ('filed_date', 'grant_date', 'event_date', or 'retrieval_timestamp').
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union
import numpy as np
import pandas as pd


class PITValidationError(Exception):
    """Raised when Point-In-Time alignment, schema constraints, or timestamp verification fails."""
    pass


class SectorCategory(str, enum.Enum):
    TECH_COMM = "tech_comm"
    HEALTHCARE = "healthcare"
    FINANCIALS = "financials"
    INDUSTRIALS_DEFENSE = "industrials_defense"
    OTHER_CONSUMER = "other_consumer"


VALID_PIT_COLUMNS = (
    "filed_date",
    "grant_date",
    "event_date",
    "retrieval_timestamp",
)


@dataclass(frozen=True)
class DriverSpec:
    """Specification of primary drivers and weights for a sector."""
    sector_category: SectorCategory
    primary_metrics: List[str]
    weights: Dict[str, float]
    required_pit_columns: Sequence[str] = VALID_PIT_COLUMNS
    description: str = ""


# Sector-native driver weight configurations per multi_sector_qualitative_pipeline_plan.md
SECTOR_DRIVER_SPECS: Dict[SectorCategory, DriverSpec] = {
    SectorCategory.TECH_COMM: DriverSpec(
        sector_category=SectorCategory.TECH_COMM,
        primary_metrics=["z_dev", "z_ai", "z_patent_quality"],
        weights={
            "z_dev": 0.35,              # GitHub PRs / active contributors
            "z_ai": 0.35,               # Hugging Face model downloads / likes / spaces
            "z_patent_quality": 0.30,   # USPTO patents forward citations & grants
        },
        description="Tech / Communication Services: GitHub + Hugging Face + USPTO Patents",
    ),
    SectorCategory.HEALTHCARE: DriverSpec(
        sector_category=SectorCategory.HEALTHCARE,
        primary_metrics=["z_clinical_trials", "z_fda_exclusivity", "z_patent_quality"],
        weights={
            "z_clinical_trials": 0.50,  # ClinicalTrials.gov phase progression
            "z_fda_exclusivity": 0.30,  # FDA patent exclusivity & approvals
            "z_patent_quality": 0.20,   # USPTO patent quality / claims
        },
        description="Healthcare / Biotech: ClinicalTrials.gov + FDA Exclusivity + USPTO Patents",
    ),
    SectorCategory.FINANCIALS: DriverSpec(
        sector_category=SectorCategory.FINANCIALS,
        primary_metrics=["z_cfpb_safety", "z_fdic_margins"],
        weights={
            "z_cfpb_safety": 0.50,      # CFPB consumer complaint safety
            "z_fdic_margins": 0.50,     # FDIC net interest margins
        },
        description="Financials: CFPB Complaints Safety + FDIC Margins",
    ),
    SectorCategory.INDUSTRIALS_DEFENSE: DriverSpec(
        sector_category=SectorCategory.INDUSTRIALS_DEFENSE,
        primary_metrics=["z_usaspending_backlog", "z_patent_quality", "z_nhtsa_safety"],
        weights={
            "z_usaspending_backlog": 0.50,  # USASpending.gov contract backlog
            "z_patent_quality": 0.30,       # USPTO patent quality / grants
            "z_nhtsa_safety": 0.20,         # NHTSA safety / inverted recalls
        },
        description="Industrials / Defense: USASpending Backlog + USPTO Patents + NHTSA Safety",
    ),
    SectorCategory.OTHER_CONSUMER: DriverSpec(
        sector_category=SectorCategory.OTHER_CONSUMER,
        primary_metrics=["z_wiki_mindshare", "z_rpo_backlog"],
        weights={
            "z_wiki_mindshare": 0.50,   # Wikimedia pageview velocity
            "z_rpo_backlog": 0.50,      # ASC 606 Remaining Performance Obligations (RPO) backlog
        },
        description="Other / Consumer: Wikimedia Pageview Velocity + ASC 606 RPO Backlog",
    ),
}

# Alias mapping for raw or alternative metric names to canonical drivers
METRIC_ALIASES: Dict[str, str] = {
    # Tech / Comm
    "github_prs": "z_dev",
    "github_contributors": "z_dev",
    "github_velocity": "z_dev",
    "dev_velocity": "z_dev",
    "z_github": "z_dev",
    "huggingface_downloads": "z_ai",
    "hf_downloads": "z_ai",
    "hf_likes": "z_ai",
    "hf_spaces": "z_ai",
    "ai_downloads": "z_ai",
    "z_huggingface": "z_ai",
    "uspto_citations": "z_patent_quality",
    "uspto_patents": "z_patent_quality",
    "patent_citations": "z_patent_quality",
    "patent_grants": "z_patent_quality",
    "patents": "z_patent_quality",
    # Healthcare
    "clinical_trials": "z_clinical_trials",
    "phase_transitions": "z_clinical_trials",
    "trial_progression": "z_clinical_trials",
    "fda_exclusivity": "z_fda_exclusivity",
    "fda_approvals": "z_fda_exclusivity",
    "fda_orange_book": "z_fda_exclusivity",
    # Financials
    "cfpb_complaints": "z_cfpb_safety",
    "cfpb_complaints_inverted": "z_cfpb_safety",
    "cfpb_safety": "z_cfpb_safety",
    "cfpb_velocity": "z_cfpb_safety",
    "fdic_margins": "z_fdic_margins",
    "fdic_nim": "z_fdic_margins",
    "net_interest_margin": "z_fdic_margins",
    # Industrials / Defense
    "usaspending": "z_usaspending_backlog",
    "usaspending_backlog": "z_usaspending_backlog",
    "defense_contracts": "z_usaspending_backlog",
    "contract_awards": "z_usaspending_backlog",
    "nhtsa_recalls": "z_nhtsa_safety",
    "nhtsa_safety": "z_nhtsa_safety",
    "vehicle_safety": "z_nhtsa_safety",
    # Other / Consumer
    "wiki_mindshare": "z_wiki_mindshare",
    "wiki_velocity": "z_wiki_mindshare",
    "wikimedia_pageviews": "z_wiki_mindshare",
    "pageviews": "z_wiki_mindshare",
    "asc606_rpo": "z_rpo_backlog",
    "rpo_backlog": "z_rpo_backlog",
    "sec_rpo": "z_rpo_backlog",
    "contract_backlog": "z_rpo_backlog",
}

# Default known company sector mappings for fast lookup
DEFAULT_TICKER_SECTOR_MAP: Dict[str, str] = {
    # Tech / Comm
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "NVDA": "Information Technology",
    "GOOGL": "Communication Services",
    "GOOG": "Communication Services",
    "META": "Communication Services",
    "SNOW": "Information Technology",
    "PLTR": "Information Technology",
    "CRM": "Information Technology",
    "ORCL": "Information Technology",
    "ADBE": "Information Technology",
    "AMD": "Information Technology",
    "INTC": "Information Technology",
    "QCOM": "Information Technology",
    "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "TMUS": "Communication Services",
    "VZ": "Communication Services",
    "T": "Communication Services",
    # Healthcare
    "PFE": "Health Care",
    "JNJ": "Health Care",
    "MRK": "Health Care",
    "ABBV": "Health Care",
    "LLY": "Health Care",
    "AMGN": "Health Care",
    "BIIB": "Health Care",
    "GILD": "Health Care",
    "MRNA": "Health Care",
    "BMY": "Health Care",
    "VRTX": "Health Care",
    # Financials
    "JPM": "Financials",
    "BAC": "Financials",
    "WFC": "Financials",
    "C": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "SOFI": "Financials",
    "UPST": "Financials",
    "AFRM": "Financials",
    "SQ": "Financials",
    "PYPL": "Financials",
    "HOOD": "Financials",
    "COIN": "Financials",
    # Industrials / Defense
    "LMT": "Industrials",
    "RTX": "Industrials",
    "NOC": "Industrials",
    "GD": "Industrials",
    "BA": "Industrials",
    "GE": "Industrials",
    "CAT": "Industrials",
    "HON": "Industrials",
    "UNP": "Industrials",
    "DE": "Industrials",
    "TSLA": "Industrials",  # or Consumer Discretionary; NHTSA safety applies
    "F": "Industrials",
    "GM": "Industrials",
    "RIVN": "Industrials",
    # Other / Consumer
    "AMZN": "Consumer Discretionary",
    "WMT": "Consumer Staples",
    "COST": "Consumer Staples",
    "TGT": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary",
    "XOM": "Energy",
    "CVX": "Energy",
    "LIN": "Materials",
    "NEE": "Utilities",
    "PLD": "Real Estate",
}


def parse_pit_timestamp(val: Any) -> datetime:
    """Parse various timestamp representations into a tz-aware or standard UTC datetime."""
    if val is None or (isinstance(val, float) and (np.isnan(val) or np.isneginf(val) or np.isposinf(val))):
        raise PITValidationError(f"Invalid timestamp value: {val!r}")
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if isinstance(val, (int, float)):
        # Epoch seconds or milliseconds
        if val > 1e11:
            val = val / 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, str):
        cleaned = val.strip()
        if cleaned.lower() in ("nan", "none", "nat", ""):
            raise PITValidationError(f"Invalid timestamp string: {val!r}")
        cleaned = cleaned.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            # Fallback to date only: YYYY-MM-DD
            dt = datetime.strptime(cleaned[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt
    raise PITValidationError(f"Cannot parse timestamp value: {val!r}")


def _is_valid_timestamp_value(val: Any) -> bool:
    """Check if a timestamp value is valid (not null, nan, nat, or empty)."""
    if val is None:
        return False
    if isinstance(val, float) and (math.isnan(val) or val != val):
        return False
    try:
        if pd.isna(val):
            return False
    except Exception:
        pass
    s = str(val).strip().lower()
    if s in ("", "nan", "none", "nat", "null"):
        return False
    return True


def validate_pit_timestamp(
    record: Union[Dict[str, Any], pd.Series],
    as_of_date: Optional[Union[str, datetime]] = None,
) -> tuple[str, str]:
    """Validate that record carries an unalterable PIT timestamp and no lookahead bias.

    Checks for presence of at least one valid PIT column:
    ('filed_date', 'grant_date', 'event_date', 'retrieval_timestamp').

    If `as_of_date` is provided, ensures PIT timestamp <= as_of_date.

    Returns:
        tuple (pit_timestamp_str, pit_column_name)
    """
    found_col: Optional[str] = None
    ts_val: Any = None

    for col in VALID_PIT_COLUMNS:
        if isinstance(record, dict):
            if col in record and _is_valid_timestamp_value(record[col]):
                found_col = col
                ts_val = record[col]
                break
        elif isinstance(record, pd.Series):
            if col in record.index and _is_valid_timestamp_value(record[col]):
                found_col = col
                ts_val = record[col]
                break

    if found_col is None:
        keys_available = list(record.keys()) if isinstance(record, dict) else list(record.index)
        raise PITValidationError(
            f"Strict PIT Violation: Record lacks unalterable timestamp column. "
            f"Expected one of {list(VALID_PIT_COLUMNS)}, but got: {keys_available}"
        )

    pit_dt = parse_pit_timestamp(ts_val)

    if as_of_date is not None:
        as_of_dt = parse_pit_timestamp(as_of_date)
        if pit_dt > as_of_dt:
            raise PITValidationError(
                f"Lookahead bias detected! PIT timestamp {pit_dt.isoformat()} ({found_col}) "
                f"is strictly after as_of_date {as_of_dt.isoformat()}."
            )

    return pit_dt.isoformat(), found_col


def filter_pit_records(
    records: Union[List[Dict[str, Any]], pd.DataFrame],
    as_of_date: Union[str, datetime],
    timestamp_col: Optional[str] = None,
) -> Union[List[Dict[str, Any]], pd.DataFrame]:
    """Filter records to only those available on or before as_of_date."""
    as_of_dt = parse_pit_timestamp(as_of_date)

    if isinstance(records, pd.DataFrame):
        if records.empty:
            return records.copy()

        # Determine timestamp column
        col = timestamp_col
        if col is None or col not in records.columns:
            for candidate in VALID_PIT_COLUMNS:
                if candidate in records.columns:
                    col = candidate
                    break

        if col is None:
            raise PITValidationError(
                f"DataFrame lacks any PIT timestamp column ({VALID_PIT_COLUMNS})."
            )

        parsed_ts = pd.to_datetime(records[col], utc=True)
        filtered = records[parsed_ts <= as_of_dt].copy()
        return filtered

    elif isinstance(records, list):
        filtered_list = []
        for r in records:
            try:
                pit_str, col_used = validate_pit_timestamp(r, as_of_date=as_of_dt)
                filtered_list.append(r)
            except PITValidationError as e:
                # If lookahead bias, drop from filtered PIT slice
                if "Lookahead bias" in str(e):
                    continue
                raise
        return filtered_list
    else:
        raise TypeError(f"Unsupported records type: {type(records)}")


class SectorDriverRouter:
    """Routes equities to sector-native traction metrics with strict PIT compliance."""

    def __init__(
        self,
        ticker_sector_map: Optional[Dict[str, str]] = None,
        missing_strategy: str = "neutral",  # 'neutral' (0.0) or 'renormalize'
    ):
        self.ticker_sector_map = dict(DEFAULT_TICKER_SECTOR_MAP)
        if ticker_sector_map:
            self.ticker_sector_map.update(ticker_sector_map)
        self.missing_strategy = missing_strategy

    @staticmethod
    def classify_sector(sector: str) -> SectorCategory:
        """Map raw GICS sector / industry string to SectorCategory."""
        if not sector or not isinstance(sector, str):
            return SectorCategory.OTHER_CONSUMER

        s = sector.lower().strip()
        # Healthcare / Biotech (check before tech to avoid 'biotech' matching 'tech')
        if any(k in s for k in ["health", "biotech", "pharma", "medical", "life science", "therapeutics"]):
            return SectorCategory.HEALTHCARE
        # Tech / Comm
        if any(k in s for k in ["information technology", "software", "communication", "telecom", "semiconductor"]) or (
            "tech" in s and "biotech" not in s
        ):
            return SectorCategory.TECH_COMM
        # Financials / Banking
        if any(k in s for k in ["finan", "bank", "credit", "insurance", "capital market", "lending", "broker"]):
            return SectorCategory.FINANCIALS
        # Industrials / Defense
        if any(k in s for k in ["industr", "defense", "aerospace", "machinery", "automotive", "transport", "freight"]):
            return SectorCategory.INDUSTRIALS_DEFENSE
        # Other / Consumer / Default
        return SectorCategory.OTHER_CONSUMER

    def get_sector_for_ticker(self, ticker: str, fallback_sector: Optional[str] = None) -> str:
        """Resolve GICS sector string for a ticker."""
        tk = ticker.upper().strip()
        if tk in self.ticker_sector_map:
            return self.ticker_sector_map[tk]
        if fallback_sector:
            return fallback_sector
        return "Other / Consumer"

    def get_driver_spec(self, sector: Union[str, SectorCategory]) -> DriverSpec:
        """Retrieve driver specification for a sector or category."""
        if isinstance(sector, SectorCategory):
            category = sector
        else:
            category = self.classify_sector(sector)
        return SECTOR_DRIVER_SPECS[category]

    def normalize_metric_inputs(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        """Map metric aliases to canonical driver names."""
        canonical: Dict[str, float] = {}
        for k, v in metrics.items():
            if v is None:
                continue
            canonical_key = METRIC_ALIASES.get(k.lower(), k.lower())
            try:
                canonical[canonical_key] = float(v)
            except (ValueError, TypeError):
                continue
        return canonical

    def calculate_traction_score(
        self,
        category: SectorCategory,
        normalized_metrics: Dict[str, float],
    ) -> tuple[float, Dict[str, float]]:
        """Calculate sector traction score T_i based on driver weights.

        Returns:
            tuple: (T_i, driver_breakdown)
        """
        spec = SECTOR_DRIVER_SPECS[category]
        weights = spec.weights
        driver_breakdown: Dict[str, float] = {}

        if self.missing_strategy == "renormalize":
            available_metrics = {m: weights[m] for m in spec.primary_metrics if m in normalized_metrics}
            total_weight = sum(available_metrics.values())
            if total_weight > 0:
                score = 0.0
                for m, w in available_metrics.items():
                    val = normalized_metrics[m]
                    effective_w = w / total_weight
                    score += effective_w * val
                    driver_breakdown[m] = val
                return float(score), driver_breakdown
            else:
                return 0.0, {m: 0.0 for m in spec.primary_metrics}
        else:
            # Default "neutral" (missing -> 0.0)
            score = 0.0
            for m in spec.primary_metrics:
                val = normalized_metrics.get(m, 0.0)
                score += weights[m] * val
                driver_breakdown[m] = val
            return float(score), driver_breakdown

    def route(
        self,
        ticker: str,
        record_or_metrics: Dict[str, Any],
        sector: Optional[str] = None,
        as_of_date: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        """Route a single ticker and its metrics to sector drivers with strict PIT validation.

        Parameters:
            ticker: Stock symbol (e.g. 'NVDA', 'PFE')
            record_or_metrics: Dict containing metrics and at least one PIT timestamp
            sector: Optional GICS sector string (falls back to ticker map)
            as_of_date: Optional PIT cutoff for backtesting validation

        Returns:
            Dict containing:
                - ticker
                - sector
                - sector_category
                - traction_score (T_i)
                - driver_breakdown
                - pit_timestamp
                - pit_timestamp_column
                - retrieval_timestamp
                - mapping_confidence
        """
        ticker_clean = ticker.upper().strip()

        # Enforce strict PIT timestamp validation
        pit_ts, pit_col = validate_pit_timestamp(record_or_metrics, as_of_date=as_of_date)

        # Resolve sector and category
        resolved_sector = sector or self.get_sector_for_ticker(ticker_clean)
        category = self.classify_sector(resolved_sector)

        # Map metric aliases to canonical keys
        canonical_metrics = self.normalize_metric_inputs(record_or_metrics)

        # Compute T_i traction score
        traction_score, driver_breakdown = self.calculate_traction_score(category, canonical_metrics)

        retrieval_ts = record_or_metrics.get(
            "retrieval_timestamp",
            datetime.now(timezone.utc).isoformat(),
        )

        return {
            "ticker": ticker_clean,
            "sector": resolved_sector,
            "sector_category": category.value,
            "traction_score": traction_score,
            "driver_breakdown": driver_breakdown,
            "pit_timestamp": pit_ts,
            "pit_timestamp_column": pit_col,
            "retrieval_timestamp": str(retrieval_ts),
            "mapping_confidence": "high" if ticker_clean in self.ticker_sector_map else "medium",
        }

    def route_dataframe(
        self,
        df: pd.DataFrame,
        as_of_date: Optional[Union[str, datetime]] = None,
    ) -> pd.DataFrame:
        """Route an entire panel DataFrame of tickers and driver observations.

        Requires:
            - 'ticker' column
            - At least one valid PIT column
            - Optional 'sector' column
        """
        if df.empty:
            return pd.DataFrame()

        if "ticker" not in df.columns:
            raise PITValidationError("DataFrame must contain 'ticker' column for routing.")

        results = []
        for _, row in df.iterrows():
            record = row.to_dict()
            ticker = str(record["ticker"])
            sec = str(record.get("sector")) if pd.notna(record.get("sector")) else None
            routed = self.route(ticker, record, sector=sec, as_of_date=as_of_date)
            results.append(routed)

        out_df = pd.DataFrame(results)
        return out_df
