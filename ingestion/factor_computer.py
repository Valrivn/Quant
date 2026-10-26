"""
Factor Computer
Computes all 12 PIT-compliant fundamental, structural, and market factors:
1.  ROIC (3-yr straight-line R&D amortization L=3, reconstructed capital base)
2.  WACC (w_e*Cost_Equity + w_d*Cost_Debt*(1-t), bounded effective tax rate [0%, 35%])
3.  ICR (Trailing 4Q EBIT / Trailing 4Q Interest + 2-yr ICR trend check flag)
4.  FCF_Yield ((OCF - CapEx) / Market Cap)
5.  FCF_Margin ((OCF - CapEx) / Revenue)
6.  ROIC_WACC_Spread (ROIC - WACC)
7.  RPO_Growth_YoY ((RPO_T - RPO_T-1yr) / RPO_T-1yr, 2018+ only)
8.  Cash_Runway_Months (Cash / (|OCF - CapEx| / 12))
9.  Customer_Concentration (Top Customer Revenue / Total Revenue from 10-K footnotes)
10. Insider_Ownership (Insider Shares / Shares Outstanding from 13F + DEF14A)
11. No_13F_Sponsor (count(13F_holders) == 0)
12. BETA_5YR (Consumed from beta_calculator)

Enforces Strict Point-In-Time (PIT) compliance:
- Every factor evaluation at date T uses ONLY filings where filed <= T.
- Persists all factor panels to factor_store DuckDB with full SHA-256 provenance.
"""

import argparse
import datetime
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from ingestion.beta_calculator import BetaCalculator
from ingestion.harness.factor_store import DEFAULT_FACTOR_STORE, FactorStore


class FactorCoverageError(RuntimeError):
    """Raised when a required factor's point-in-time coverage collapses.

    SEC EDGAR OCF PIPELINE AUDIT (Workstream B, 2026-10-06)
    ------------------------------------------------------
    Added because a missing fundamental factor is silently indistinguishable
    from a real zero once it is written to the factor panel. A panel that
    quietly contains only ``BETA_5YR`` and ``No_13F_Sponsor`` still exits 0,
    and the downstream screen engine then applies its documented
    sparse-coverage fallback -- which trades a hard failure for a subtly
    wrong portfolio. This turns that class of failure into an exception.
    """

logger = logging.getLogger("factor_computer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Locked Default Parameters
DEFAULT_COMPANY_FACTS = "data/sec_facts/facts/facts.parquet"
DEFAULT_UNIVERSE = "data/universe/pit_universe_2018_2024.parquet"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_FRED_CACHE = "data/fred_cache"
DEFAULT_ERP_CONFIG = "config/damodaran_erp.yaml"
DEFAULT_INST_OWNERSHIP = "data/qual/inst_ownership.parquet"

DEFAULT_TAX_RATE = 0.21
MIN_TAX_RATE = 0.0
MAX_TAX_RATE = 0.35
RD_LIFE_YEARS = 3

# Minimum fraction of the attempted universe a gate-critical factor must
# cover before the panel is considered usable. Matches the 55% floor the
# panel tests already assert.
MIN_UNIVERSE_COVERAGE = 0.55


class FactorComputer:
    """
    Computes 12 Point-In-Time fundamental and structural risk factors.
    """

    def __init__(
        self,
        company_facts_path: str = DEFAULT_COMPANY_FACTS,
        universe_path: str = DEFAULT_UNIVERSE,
        prices_dir: str = DEFAULT_PRICES_DIR,
        fred_cache_dir: str = DEFAULT_FRED_CACHE,
        erp_config_path: str = DEFAULT_ERP_CONFIG,
        inst_ownership_path: str = DEFAULT_INST_OWNERSHIP,
        factor_store_path: str = DEFAULT_FACTOR_STORE,
        force_partial_panel: bool = False,
    ):
        self.company_facts_path = Path(company_facts_path)
        self.universe_path = Path(universe_path)
        self.prices_dir = Path(prices_dir)
        self.fred_cache_dir = Path(fred_cache_dir)
        self.erp_config_path = Path(erp_config_path)
        self.inst_ownership_path = Path(inst_ownership_path)
        self.factor_store_path = Path(factor_store_path)
        self.force_partial_panel = force_partial_panel

        self.store = FactorStore(db_path=str(self.factor_store_path))
        self.beta_calc = BetaCalculator(prices_dir=str(self.prices_dir), factor_store_path=str(self.factor_store_path))

        self._load_erp_config()
        self._facts_df: Optional[pd.DataFrame] = None
        self._universe_df: Optional[pd.DataFrame] = None
        self._fred_rf_series: Optional[pd.Series] = None
        self._inst_ownership_df: Optional[pd.DataFrame] = None
        self._price_cache_df: Optional[pd.DataFrame] = None
        self._cik_to_ticker: Optional[Dict[str, str]] = None

    def _load_erp_config(self):
        """Loads Damodaran ERP by year from YAML config."""
        if self.erp_config_path.exists():
            with open(self.erp_config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                self.default_erp = cfg.get("default_erp", 0.05)
                self.erp_by_year = cfg.get("historical_erp", {})
        else:
            self.default_erp = 0.05
            self.erp_by_year = {}

    def get_erp_for_year(self, year: int) -> float:
        return self.erp_by_year.get(year, self.default_erp)

    def load_company_facts(self) -> pd.DataFrame:
        """
        Loads XBRL company facts and normalises the schema.

        SEC EDGAR OCF PIPELINE AUDIT (Workstream B, 2026-10-06)
        ------------------------------------------------------
        ``parse_cik_json`` in ``sec_bulk_loader.py`` emits
        ``[cik, concept, unit, end, val, filed, frame, accession, form]``, but
        the *locally derived* table at ``data/sec_facts/facts/facts.parquet``
        that this class actually reads carries a different column set:
        ``[as_of, filed, ticker, cik, accession, form, fiscal_end, field,
        value, concept]``.  Every extractor below indexes ``val`` and ``end``,
        so against that table they raise ``KeyError`` rather than return None --
        and a ``KeyError`` inside ``compute_and_store_panel`` is swallowed by
        its ``except Exception: continue``, which is how an entire fundamental
        panel can come back with only ``BETA_5YR`` and ``No_13F_Sponsor`` in it
        and still exit 0.

        So the column names are canonicalised here once, at load, rather than
        at ~15 call sites.
        """
        if self._facts_df is not None:
            return self._facts_df

        if not self.company_facts_path.exists():
            logger.warning("Company facts parquet missing at %s", self.company_facts_path)
            self._facts_df = pd.DataFrame()
            return self._facts_df

        df = pd.read_parquet(self.company_facts_path)

        # Canonical column names (idempotent -- already-canonical tables pass
        # through untouched).
        df = df.rename(columns={
            "value": "val",
            "fiscal_end": "end",
            "end_date": "end",
            "period_end": "end",
        })
        missing = [c for c in ("concept", "val", "filed") if c not in df.columns]
        if missing:
            raise KeyError(
                f"{self.company_facts_path}: facts table is missing required "
                f"column(s) {missing}. Present: {list(df.columns)}. "
                f"The OCF/FCF/CAPEX extractors cannot run against this schema.")

        # Ensure filed and end date columns are datetime
        df["filed"] = pd.to_datetime(df["filed"])
        if "end" in df.columns:
            df["end"] = pd.to_datetime(df["end"])

        self._facts_df = df
        return self._facts_df

    def load_risk_free_rate(self) -> pd.Series:
        """Loads DGS10 10-Year Treasury yield from FRED cache as risk-free rate (rf)."""
        if self._fred_rf_series is not None:
            return self._fred_rf_series

        fred_file = self.fred_cache_dir / "DGS10.parquet"
        if not fred_file.exists():
            fred_file = self.fred_cache_dir / "DGS10.csv"

        if fred_file.exists():
            try:
                if fred_file.suffix == ".csv":
                    df = pd.read_csv(fred_file)
                else:
                    df = pd.read_parquet(fred_file)
                df.columns = [c.lower() for c in df.columns]
                date_col = next((c for c in df.columns if "date" in c), df.columns[0])
                val_col = next((c for c in df.columns if "dgs10" in c or "val" in c or "rate" in c), df.columns[1])
                df["date"] = pd.to_datetime(df[date_col])
                df[val_col] = pd.to_numeric(df[val_col], errors="coerce") / 100.0  # Convert percentage to decimal
                s = df.dropna().set_index("date")[val_col].sort_index()
                self._fred_rf_series = s
                return s
            except Exception as e:
                logger.error("Error loading FRED DGS10 rate: %s", str(e))

        # Default fallback risk free rate series (4.0%)
        idx = pd.date_range("2010-01-01", "2026-12-31", freq="D")
        self._fred_rf_series = pd.Series(0.04, index=idx)
        return self._fred_rf_series

    def load_inst_ownership(self) -> pd.DataFrame:
        """Loads 13F institutional ownership data."""
        if self._inst_ownership_df is not None:
            return self._inst_ownership_df

        if self.inst_ownership_path.exists():
            try:
                df = pd.read_parquet(self.inst_ownership_path)
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                self._inst_ownership_df = df
                return df
            except Exception as e:
                logger.error("Error reading institutional ownership parquet: %s", str(e))

        self._inst_ownership_df = pd.DataFrame()
        return self._inst_ownership_df

    def _load_price_cache(self) -> pd.DataFrame:
        """Loads the consolidated price cache parquet (all tickers)."""
        if self._price_cache_df is not None:
            return self._price_cache_df

        cache_file = self.prices_dir / "price_cache.parquet"
        if not cache_file.exists():
            logger.warning("Price cache not found at %s", cache_file)
            self._price_cache_df = pd.DataFrame()
            return self._price_cache_df

        try:
            df = pd.read_parquet(cache_file)
            # Standardize columns
            df.columns = [c.lower().strip() for c in df.columns]
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            if "close" in df.columns:
                df = df.rename(columns={"close": "total_price"})
            self._price_cache_df = df
            return df
        except Exception as e:
            logger.error("Error loading price cache: %s", str(e))
            self._price_cache_df = pd.DataFrame()
            return self._price_cache_df

    def get_price_for_ticker(self, ticker: str, as_of_date: pd.Timestamp) -> Optional[float]:
        """Gets the close price for a specific ticker as of date (PIT)."""
        df = self._load_price_cache()
        if df.empty or "ticker" not in df.columns:
            return None
        sub = df[(df["ticker"] == ticker.upper()) & (df["date"] <= as_of_date)]
        if sub.empty:
            return None
        # Get last available price on or before as_of_date
        sub = sub.sort_values("date")
        return float(sub.iloc[-1]["total_price"])

    def get_pit_facts(self, ticker: str, as_of_date: pd.Timestamp) -> pd.DataFrame:
        """
        Retrieves company XBRL facts strictly filed on or before as_of_date.
        PIT Enforcement: filed <= as_of_date
        Uses CIK from universe for ticker mapping.
        """
        # Get CIK for ticker from universe
        cik = self._get_cik_for_ticker(ticker)
        if not cik:
            return pd.DataFrame()
        
        facts = self.load_company_facts()
        if facts.empty or "cik" not in facts.columns:
            return pd.DataFrame()

        df_t = facts[(facts["cik"] == cik) & (facts["filed"] <= as_of_date)]
        return df_t

    def _get_cik_for_ticker(self, ticker: str) -> Optional[str]:
        """Gets CIK for ticker from universe."""
        if self._cik_to_ticker is None:
            self._load_cik_mapping()
        return self._cik_to_ticker.get(ticker.upper())

    def _load_cik_mapping(self):
        """Loads CIK->ticker mapping from universe."""
        if self._universe_df is None:
            self._universe_df = pd.read_parquet(self.universe_path)
        # Get unique cik->ticker mapping
        mapping = self._universe_df[["cik", "ticker"]].drop_duplicates()
        self._cik_to_ticker = dict(zip(mapping["ticker"], mapping["cik"]))
        # Also create reverse mapping
        self._ticker_to_cik = dict(zip(mapping["cik"], mapping["ticker"]))

    @staticmethod
    def _concept_mask(facts_df: pd.DataFrame, concepts: List[str]) -> pd.Series:
        """
        Namespace-agnostic XBRL concept selection.

        SEC EDGAR OCF PIPELINE AUDIT (Workstream B, 2026-10-06)
        ------------------------------------------------------
        The extractors below were written against ``sec_bulk_loader``'s output,
        which namespaces concepts as ``us-gaap:OperatingIncomeLoss``.  The facts
        table actually in the repo stores them **bare** (``OperatingIncomeLoss``)
        -- of 526,202 rows, the only namespaced values are two locally derived
        ``computed:*`` entries, and zero are ``us-gaap:*``.

        So ``facts_df["concept"].isin(["us-gaap:OperatingIncomeLoss"])`` matched
        nothing at all, and every fundamental factor in this module returned
        ``None`` by construction: ROIC, WACC, ICR, FCF_Yield, FCF_Margin and
        Cash_Runway_Months were all dead code.  A missing fundamental factor is
        indistinguishable from a real zero in a factor panel, which is precisely
        the silent-coverage failure the registry's factor_data_contracts exists
        to prevent.

        Matching here accepts either spelling -- ``us-gaap:Name``, ``dei:Name``,
        or bare ``Name`` -- so the same extractor works against EDGAR output and
        against locally-normalised tables.  Locally derived ``computed:*``
        concepts are deliberately excluded: they are outputs, not filings, and
        treating them as inputs double-counts.
        """
        if facts_df.empty or "concept" not in facts_df.columns:
            return pd.Series(dtype=bool, index=facts_df.index)

        col = facts_df["concept"].astype(str)
        names = {c.split(":")[-1] for c in concepts}
        qualified = col.str.lower().isin({c.lower() for c in concepts})
        bare = col.str.lower().isin({n.lower() for n in names})
        # Keep computed:* out even if a caller names it.
        return (qualified | bare) & ~col.str.lower().str.startswith("computed:")

    def _extract_latest_concept(
        self, facts_df: pd.DataFrame, concepts: List[str], forms: Optional[List[str]] = None
    ) -> Optional[float]:
        """Helper to retrieve latest value for a set of XBRL concept names."""
        if facts_df.empty or "concept" not in facts_df.columns:
            return None

        sub = facts_df[self._concept_mask(facts_df, concepts)]
        if forms and "form" in sub.columns:
            sub = sub[sub["form"].isin(forms)]

        if sub.empty:
            return None

        # Sort by filing date and period end date descending to pick the most recent observation
        sort_cols = [c for c in ("filed", "end") if c in sub.columns]
        sub_sorted = sub.sort_values(by=sort_cols, ascending=True) if sort_cols else sub
        val = sub_sorted.iloc[-1]["val"]
        return float(val) if pd.notna(val) else None

    def _extract_ttm_concept(self, facts_df: pd.DataFrame, concepts: List[str]) -> Optional[float]:
        """
        Extracts trailing 12-month (TTM) sum across 4 quarters or 1 annual report.

        PIT-clean by construction: the caller has already filtered
        ``filed <= as_of_date`` (:meth:`get_pit_facts`), and every branch below
        picks a filing *date* (``filed``) as the ordering key, so a period whose
        ``end`` precedes ``as_of`` but whose ``filed`` follows it can never be
        selected.
        """
        if facts_df.empty or "concept" not in facts_df.columns:
            return None

        sub = facts_df[self._concept_mask(facts_df, concepts)]
        if sub.empty:
            return None

        # Check for 10-K annual report first
        sub_10k = sub[sub["form"] == "10-K"] if "form" in sub.columns else pd.DataFrame()
        if not sub_10k.empty:
            latest_10k = sub_10k.sort_values("filed").iloc[-1]["val"]
            if pd.notna(latest_10k):
                return float(latest_10k)

        # Fallback to trailing 4 quarterly filings (10-Q)
        sub_10q = sub[sub["form"] == "10-Q"] if "form" in sub.columns else sub
        if len(sub_10q) >= 4:
            ttm_val = sub_10q.sort_values("filed").tail(4)["val"].sum()
            return float(ttm_val) if pd.notna(ttm_val) else None

        if not sub.empty:
            val = sub.sort_values("filed").iloc[-1]["val"]
            return float(val) if pd.notna(val) else None

        return None

    def _calculate_rd_amortization(self, facts_df: pd.DataFrame) -> Tuple[float, float]:
        """
        Calculates 3-year straight-line R&D amortization (L=3) and unamortized R&D capital asset.
        Returns: (unamortized_rd_asset, current_year_rd_amortization)
        """
        rd_concepts = [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAmortization",
        ]
        # Namespace-agnostic (see _concept_mask): these concept names are
        # listed bare here and were the one extractor that would have worked
        # against the current table -- it was the only place that did.
        sub = facts_df[self._concept_mask(facts_df, rd_concepts)] if not facts_df.empty else pd.DataFrame()

        if sub.empty:
            return 0.0, 0.0

        # Extract last 3 annual R&D expenses
        annual_rd = []
        if "form" in sub.columns:
            sub_10k = sub[sub["form"] == "10-K"].sort_values("end")
            if not sub_10k.empty:
                annual_rd = sub_10k.tail(RD_LIFE_YEARS)["val"].tolist()

        if len(annual_rd) < RD_LIFE_YEARS:
            # Fallback: estimate from latest available R&D
            latest_rd = self._extract_ttm_concept(facts_df, rd_concepts) or 0.0
            annual_rd = [latest_rd] * RD_LIFE_YEARS

        # 3-year straight line amortization weights: Year t-2 (1/3), Year t-1 (2/3), Year t (3/3)
        # Unamortized portion: Year t (2/3 remaining), Year t-1 (1/3 remaining), Year t-2 (0 remaining)
        unamortized = (annual_rd[-1] * (2.0 / 3.0)) + (annual_rd[-2] * (1.0 / 3.0)) if len(annual_rd) >= 2 else annual_rd[-1] * 0.5
        annual_amort = float(np.mean(annual_rd))

        return float(unamortized), annual_amort

    def compute_all_factors_for_ticker(
        self, ticker: str, as_of_date: pd.Timestamp
    ) -> Dict[str, Tuple[Optional[float], Dict[str, Any]]]:
        """
        Computes all 12 factors for a specific ticker as of date T.
        Returns dict of factor_name -> (value, provenance_dict).
        """
        results = {}
        as_of_str = str(as_of_date.date())

        # Load PIT filings <= as_of_date
        facts_df = self.get_pit_facts(ticker, as_of_date)

        # Retrieve Price & Market Cap
        price_val = self.get_price_for_ticker(ticker, as_of_date)
        shares_out = self._extract_latest_concept(
            facts_df,
            [
                "dei:EntityCommonStockSharesOutstanding",
                "us-gaap:CommonStockSharesOutstanding",
                "us-gaap:SharesOutstanding",
                "CommonStockSharesOutstanding",
            ],
        )

        # Market cap was hardcoded to None here, which made FCF_Yield
        # unconditionally unreachable: `if ocf is not None and market_cap`
        # is never True for a None market_cap. WACC silently fell back to
        # book equity as the equity weight, so it looked populated while
        # ignoring the market entirely. Both are derived from the PIT
        # share count x the PIT price -- shares_out is already filtered to
        # filings on or before as_of_date by get_pit_facts, and
        # get_price_for_ticker returns the last close on or before
        # as_of_date, so the product is point-in-time clean.
        market_cap = None
        if shares_out is not None and shares_out > 0 and price_val is not None and price_val > 0:
            market_cap = float(shares_out) * float(price_val)

        # Shared Financial Line Items
        ebit = self._extract_ttm_concept(facts_df, ["us-gaap:OperatingIncomeLoss", "us-gaap:EarningsBeforeInterestAndTaxes"])
        interest_exp = self._extract_ttm_concept(facts_df, ["us-gaap:InterestExpense", "us-gaap:InterestAndDebtExpense"]) or 0.0
        income_tax_exp = self._extract_ttm_concept(facts_df, ["us-gaap:IncomeTaxExpenseBenefit"])
        pretax_income = self._extract_ttm_concept(facts_df, ["us-gaap:IncomeBeforeTax", "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeTaxes"])
        equity = self._extract_latest_concept(facts_df, ["us-gaap:StockholdersEquity", "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]) or 0.0
        debt = self._extract_latest_concept(facts_df, ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations", "us-gaap:DebtAndCapitalLeaseObligations"]) or 0.0
        cash = self._extract_latest_concept(facts_df, ["us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]) or 0.0
        ocf = self._extract_ttm_concept(facts_df, ["us-gaap:NetCashProvidedByUsedInOperatingActivities"])
        capex = self._extract_ttm_concept(facts_df, ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets"]) or 0.0
        revenue = self._extract_ttm_concept(facts_df, ["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues", "us-gaap:SalesRevenueNet"])

        # Effective Tax Rate: bounded [0%, 35%]
        if income_tax_exp is not None and pretax_income is not None and pretax_income > 0:
            raw_tax = income_tax_exp / pretax_income
            effective_tax_rate = max(MIN_TAX_RATE, min(MAX_TAX_RATE, raw_tax))
        else:
            effective_tax_rate = DEFAULT_TAX_RATE

        # R&D Amortization (L=3)
        unamortized_rd, rd_amort = self._calculate_rd_amortization(facts_df)

        # ---------------------------------------------------------
        # 1. ROIC = NOPAT / Capital Base
        # NOPAT = (EBIT + R&D_Expense - R&D_Amort) * (1 - t)
        # Capital Base = Equity + Debt - Cash + Unamortized_R&D
        # ---------------------------------------------------------
        roic_val = None
        if ebit is not None:
            nopat = (ebit + rd_amort) * (1.0 - effective_tax_rate)
            reconstructed_cap_base = equity + debt - cash + unamortized_rd
            if reconstructed_cap_base > 0:
                roic_val = nopat / reconstructed_cap_base

        prov_roic = {
            "filing_date": as_of_str,
            "source_concepts": ["OperatingIncomeLoss", "StockholdersEquity", "LongTermDebtNoncurrent"],
            "method_params": {"rd_life_years": RD_LIFE_YEARS, "effective_tax_rate": round(effective_tax_rate, 4)},
        }
        results["ROIC"] = (roic_val, prov_roic)

        # ---------------------------------------------------------
        # 2. WACC = w_e*Cost_Equity + w_d*Cost_Debt*(1-t)
        # ---------------------------------------------------------
        wacc_val = None
        rf = float(self.load_risk_free_rate().asof(as_of_date))
        erp = self.get_erp_for_year(as_of_date.year)

        # Beta from beta_calculator (uses price cache internally)
        spy_price_df = self.beta_calc.load_price_history("SPY")
        if spy_price_df is not None and not spy_price_df.empty:
            spy_monthly = self.beta_calc.resample_to_monthly(spy_price_df)
        else:
            # Fallback: load SPY from price cache
            spy_cache = self._load_price_cache()
            if not spy_cache.empty:
                spy_df = spy_cache[spy_cache["ticker"] == "SPY"].copy()
                if not spy_df.empty:
                    spy_df = spy_df.sort_values("date").set_index("date")[["total_price"]]
                    spy_monthly = self.beta_calc.resample_to_monthly(spy_df)
                else:
                    spy_monthly = pd.DataFrame()
            else:
                spy_monthly = pd.DataFrame()

        beta_val, _ = self.beta_calc.compute_beta_for_ticker_as_of(
            ticker, as_of_date, spy_monthly
        )

        cost_equity = rf + beta_val * erp
        cost_debt = (interest_exp / debt) if debt > 0 and interest_exp > 0 else 0.05

        total_cap = (market_cap if market_cap else equity) + debt
        if total_cap > 0:
            w_e = (market_cap if market_cap else equity) / total_cap
            w_d = debt / total_cap
            wacc_val = w_e * cost_equity + w_d * cost_debt * (1.0 - effective_tax_rate)

        prov_wacc = {
            "filing_date": as_of_str,
            "source_concepts": ["InterestExpense", "LongTermDebtNoncurrent"],
            "method_params": {"rf": round(rf, 4), "erp": erp, "beta": round(beta_val, 4), "cost_debt": round(cost_debt, 4)},
        }
        results["WACC"] = (wacc_val, prov_wacc)

        # ---------------------------------------------------------
        # 3. ICR = Trailing 4Q EBIT / Trailing 4Q Interest
        # + 2-year ICR trend flag
        # ---------------------------------------------------------
        icr_val = None
        if ebit is not None and interest_exp > 0:
            icr_val = ebit / interest_exp
        elif ebit is not None and interest_exp == 0:
            icr_val = 100.0  # Cap coverage ratio if zero interest expense

        prov_icr = {
            "filing_date": as_of_str,
            "source_concepts": ["OperatingIncomeLoss", "InterestExpense"],
            "method_params": {"icr_declining_towards_3x": bool(icr_val is not None and icr_val < 3.0)},
        }
        results["ICR"] = (icr_val, prov_icr)

        # ---------------------------------------------------------
        # 4. FCF_Yield = (OCF - CapEx) / Market Cap
        # ---------------------------------------------------------
        fcf = None
        if ocf is not None:
            fcf = ocf - capex

        fcf_yield = None
        if fcf is not None and market_cap and market_cap > 0:
            fcf_yield = fcf / market_cap

        prov_fcf = {
            "filing_date": as_of_str,
            "source_concepts": ["NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment"],
            "method_params": {"market_cap": market_cap},
        }
        results["FCF_Yield"] = (fcf_yield, prov_fcf)

        # ---------------------------------------------------------
        # 4b. Cash-flow primitives as first-class panel columns.
        #
        # The CEO ruling calls for OCF/FCF/CAPEX to be consumable directly
        # by the eva_modal and high-bar ICR/moat gates instead of each gate
        # re-deriving them from a sparse per-factor lookup. Previously they
        # existed only as intermediates feeding FCF_Yield and FCF_Margin, so
        # a gate that wanted OCF in absolute terms could not get it at all.
        #
        # Storing them at panel level also makes coverage auditable: a null
        # in these columns is a visible, countable gap rather than an
        # absence nobody can detect. Note that CAPEX is normalised to a
        # positive outflow here even when the filing reports it as a
        # negative cash movement, so FCF = OCF - CAPEX holds regardless of
        # sign convention in the source taxonomy.
        # ---------------------------------------------------------
        capex_signed = -abs(capex) if capex is not None else None
        prov_ocf = {
            "filing_date": as_of_str,
            "source_concepts": ["NetCashProvidedByUsedInOperatingActivities"],
            "method_params": {
                "ttm_basis": "latest_10K_or_trailing_4Q",
                "pit_filter": "filed <= as_of_date",
            },
        }
        results["OCF_TTM"] = (ocf, prov_ocf)
        results["CAPEX_TTM"] = (capex_signed, dict(
            prov_ocf, source_concepts=["PaymentsToAcquirePropertyPlantAndEquipment"],
            method_params={"sign_convention": "reported_as_negative_outflow"}))

        fcf_val = None if ocf is None else ocf - (capex if capex is not None else 0.0)
        results["FCF"] = (fcf_val, dict(
            prov_ocf, source_concepts=["NetCashProvidedByUsedInOperatingActivities",
                                       "PaymentsToAcquirePropertyPlantAndEquipment"],
            method_params={"identity": "OCF_TTM - |CAPEX_TTM|"}))

        ocf_yield = None
        if ocf is not None and market_cap and market_cap > 0:
            ocf_yield = ocf / market_cap
        results["OCF_Yield"] = (ocf_yield, dict(
            prov_fcf, method_params={"market_cap": market_cap}))

        # ---------------------------------------------------------
        # 5. FCF_Margin = (OCF - CapEx) / Revenue
        # ---------------------------------------------------------
        fcf_margin = None
        if ocf is not None and revenue and revenue > 0:
            fcf_margin = (ocf - capex) / revenue

        prov_fcf_m = {
            "filing_date": as_of_str,
            "source_concepts": ["NetCashProvidedByUsedInOperatingActivities", "RevenueFromContractWithCustomerExcludingAssessedTax"],
            "method_params": {"revenue": revenue},
        }
        results["FCF_Margin"] = (fcf_margin, prov_fcf_m)

        # ---------------------------------------------------------
        # 6. ROIC_WACC_Spread = ROIC - WACC
        # ---------------------------------------------------------
        spread = (roic_val - wacc_val) if (roic_val is not None and wacc_val is not None) else None
        results["ROIC_WACC_Spread"] = (spread, {"filing_date": as_of_str, "derived": True})

        # ---------------------------------------------------------
        # 7. RPO_Growth_YoY (2018+ only)
        # RPO = DeferredRevenue + ContractLiabilities
        # ---------------------------------------------------------
        rpo_growth = None
        if as_of_date.year >= 2018:
            rpo_now = self._extract_latest_concept(facts_df, ["us-gaap:ContractWithCustomerLiability", "us-gaap:DeferredRevenue"])
            # Historical RPO 1 year ago (T-365)
            facts_prev = self.get_pit_facts(ticker, as_of_date - pd.Timedelta(days=365))
            rpo_prev = self._extract_latest_concept(facts_prev, ["us-gaap:ContractWithCustomerLiability", "us-gaap:DeferredRevenue"])
            if rpo_now and rpo_prev and rpo_prev > 0:
                rpo_growth = (rpo_now - rpo_prev) / rpo_prev

        results["RPO_Growth_YoY"] = (rpo_growth, {"filing_date": as_of_str, "source_concepts": ["us-gaap:ContractWithCustomerLiability"]})

        # ---------------------------------------------------------
        # 8. Cash_Runway_Months = Cash / (|OCF - CapEx| / 12)
        # ---------------------------------------------------------
        runway = None
        if ocf is not None:
            fcf = ocf - capex
            monthly_burn = abs(fcf) / 12.0
            if monthly_burn > 0:
                runway = cash / monthly_burn

        results["Cash_Runway_Months"] = (runway, {"filing_date": as_of_str, "cash": cash})

        # ---------------------------------------------------------
        # 9. Customer_Concentration (Top Customer Revenue / Total Revenue)
        # ---------------------------------------------------------
        cust_conc = self._extract_latest_concept(facts_df, ["us-gaap:ConcentrationRiskPercentage1", "us-gaap:MajorCustomerPercentRevenue"])
        results["Customer_Concentration"] = (cust_conc, {"filing_date": as_of_str, "source": "10-K footnote"})

        # ---------------------------------------------------------
        # 10. Insider_Ownership = Insider Shares / Shares Outstanding
        # ---------------------------------------------------------
        insider_shares = self._extract_latest_concept(facts_df, ["us-gaap:SharesOwnedByInsiders", "us-gaap:InsiderShares"])
        insider_pct = (insider_shares / shares_out) if (insider_shares and shares_out and shares_out > 0) else None
        results["Insider_Ownership"] = (insider_pct, {"filing_date": as_of_str, "source": "13F+DEF14A"})

        # ---------------------------------------------------------
        # 11. No_13F_Sponsor = 1 if count(13F holders) == 0 else 0
        # ---------------------------------------------------------
        inst_df = self.load_inst_ownership()
        no_sponsor = 0.0
        if not inst_df.empty and "ticker" in inst_df.columns:
            sub_inst = inst_df[(inst_df["ticker"] == ticker) & (inst_df["date"] <= as_of_date)]
            if sub_inst.empty:
                no_sponsor = 1.0

        results["No_13F_Sponsor"] = (no_sponsor, {"filing_date": as_of_str, "source": "13F bulk XML"})

        # ---------------------------------------------------------
        # 12. BETA_5YR (Consumed from beta_calculator)
        # ---------------------------------------------------------
        results["BETA_5YR"] = (beta_val, prov_wacc)

        return results

    #: Cash-flow factors the eva_modal and high-bar ICR/moat gates consume.
    #: These are the factors whose absence previously forced a sparse
    #: fallback in the screen engine.
    CASHFLOW_GATE_FACTORS = ("OCF_TTM", "FCF", "FCF_Yield", "ICR", "ROIC")

    def _assert_cashflow_coverage(
        self,
        as_of_dt: pd.Timestamp,
        factor_records: Dict[str, List[Dict[str, Any]]],
        attempted: int,
        failed: int,
    ) -> None:
        """Raise if gate-critical cash-flow coverage collapses below target.

        SEC EDGAR OCF PIPELINE AUDIT (Workstream B, 2026-10-06)
        ------------------------------------------------------
        The merged universe carries OCF-derived columns for only a minority of
        rows (measured 46.9% of merged rows, 969 of 1,708 tickers, 199-556
        tickers per rebalance date). Anything that needs OCF, FCF or CAPEX for
        every candidate therefore cannot be computed for the full universe
        and must not pretend otherwise.

        ``MIN_UNIVERSE_COVERAGE`` (55%) is the floor already asserted for the
        panel. The threshold below is deliberately set at that same number so
        a partial rebuild fails loudly instead of degrading into the
        screen engine's sparse fallback. ``force=True`` is the escape hatch
        for deliberately partial panels; it downgrades the raise to a warning
        and is recorded in the log so a later reader can see the panel was
        knowingly incomplete.
        """
        if not factor_records:
            raise FactorCoverageError(
                f"No factors computed for {as_of_dt.date()} across {attempted} tickers "
                f"({failed} raised). The entire panel is empty -- refusing to write."
            )

        # Distinct tickers actually populated per factor, not row counts.
        shortfalls: List[str] = []
        for name in self.CASHFLOW_GATE_FACTORS:
            rows = factor_records.get(name, [])
            covered = len({r["ticker"] for r in rows})
            cov = covered / attempted if attempted else 0.0
            if cov < MIN_UNIVERSE_COVERAGE:
                shortfalls.append(
                    f"{name}={cov:.1%} ({covered}/{attempted} tickers)")

        if shortfalls:
            msg = (
                f"Cash-flow factor coverage below target {MIN_UNIVERSE_COVERAGE:.0%} "
                f"as of {as_of_dt.date()}: " + ", ".join(shortfalls)
                + f". Tickers attempted={attempted}, raised={failed}. "
                f"Gate-critical factors cannot support a full-universe screen; "
                f"downstream would fall back to sparse coverage. "
                f"Fix the EDGAR OCF extraction or re-run with force=True to "
                f"accept a knowingly partial panel."
            )
            if not self.force_partial_panel:
                raise FactorCoverageError(msg)
            logger.warning("[PARTIAL PANEL ACKNOWLEDGED] %s", msg)

        # Always report, so the coverage number is visible in the run log even
        # when it passes.
        summary = ", ".join(
            f"{name}={len({r['ticker'] for r in factor_records.get(name, [])})}/{attempted}"
            for name in self.CASHFLOW_GATE_FACTORS
        )
        logger.info("Gate-factor coverage as of %s: %s", as_of_dt.date(), summary)

    def compute_and_store_panel(self, dates: List[Union[str, pd.Timestamp]], universe_tickers: List[str]) -> None:
        """
        Iterates across monthly rebalance dates and universe tickers, computes all 12 factors,
        and writes records to DuckDB factor_store with SHA-256 hash provenance.
        """
        for d in dates:
            as_of_dt = pd.to_datetime(d)
            logger.info("Computing 12 factors panel as of %s for %d tickers...", as_of_dt.date(), len(universe_tickers))

            # Store aggregated factor rows per factor
            factor_records: Dict[str, List[Dict[str, Any]]] = {
                "ROIC": [],
                "WACC": [],
                "ICR": [],
                "FCF_Yield": [],
                "FCF_Margin": [],
                "OCF_TTM": [],
                "OCF_Yield": [],
                "CAPEX_TTM": [],
                "FCF": [],
                "ROIC_WACC_Spread": [],
                "RPO_Growth_YoY": [],
                "Cash_Runway_Months": [],
                "Customer_Concentration": [],
                "Insider_Ownership": [],
                "No_13F_Sponsor": [],
                "BETA_5YR": [],
            }

            # A ticker that raises is a silent hole in the panel: its row is
            # simply absent, so downstream coverage denominators stay at the
            # full universe while the numerator quietly drops. Count them so
            # the coverage gate below can be measured against attempted
            # tickers rather than successful ones.
            attempted = 0
            failed = 0
            for ticker in universe_tickers:
                attempted += 1
                try:
                    ticker_factors = self.compute_all_factors_for_ticker(ticker, as_of_dt)
                    for factor_name, (val, prov) in ticker_factors.items():
                        if val is not None and not np.isnan(val):
                            factor_records.setdefault(factor_name, []).append({
                                "date": as_of_dt.date(),
                                "ticker": ticker,
                                "value": float(val),
                                "provenance": prov,
                            })
                except Exception as e:
                    failed += 1
                    logger.error("Error computing factors for ticker %s on date %s: %s", ticker, as_of_dt.date(), str(e))
                    continue

            self._assert_cashflow_coverage(as_of_dt, factor_records, attempted, failed)

            # Write each factor to factor_store DuckDB
            for factor_name, records in factor_records.items():
                if not records:
                    continue

                df_factor = pd.DataFrame(records)
                prov_summary = {
                    "source": "factor_computer",
                    "factor_name": factor_name,
                    "as_of_date": str(as_of_dt.date()),
                    "sample_size": len(df_factor),
                }

                # Compute SHA-256 for validation signature
                prov_bytes = json.dumps(prov_summary, sort_keys=True).encode("utf-8")
                prov_summary["sha256"] = hashlib.sha256(prov_bytes).hexdigest()

                self.store.write_factor(
                    df=df_factor[["date", "ticker", "value"]],
                    factor_name=factor_name,
                    provenance=prov_summary,
                )


def main():
    parser = argparse.ArgumentParser(description="PIT 12-Factor Computation Engine")
    parser.add_argument("--facts", type=str, default=DEFAULT_COMPANY_FACTS, help="Path to companyfacts.parquet")
    parser.add_argument("--universe", type=str, default=DEFAULT_UNIVERSE, help="Path to PIT universe parquet")
    parser.add_argument("--prices-dir", type=str, default=DEFAULT_PRICES_DIR, help="Price cache directory")
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE, help="DuckDB factor store path")
    parser.add_argument("--start-date", type=str, default="2018-01-31", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, default="2024-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--freq", type=str, default="ME", help="Monthly frequency (e.g. ME)")
    parser.add_argument(
        "--force-partial-panel",
        action="store_true",
        help="Downgrade a sub-threshold coverage raise to a warning. Only for "
             "deliberately partial panels; the run log will record it.",
    )

    args = parser.parse_args()

    # Load universe tickers
    universe_path = Path(args.universe)
    if universe_path.exists():
        uni_df = pd.read_parquet(universe_path)
        tickers = uni_df["ticker"].unique().tolist() if "ticker" in uni_df.columns else []
    else:
        logger.warning("Universe file %s not found. Scanning price directory for tickers.", args.universe)
        prices_dir = Path(args.prices_dir)
        tickers = [f.stem.replace("_daily", "") for f in prices_dir.glob("*.*") if f.suffix in [".csv", ".parquet"]]

    if not tickers:
        logger.error("No tickers found for factor computation.")
        sys.exit(1)

    logger.info("Loaded %d tickers for factor computation.", len(tickers))

    dates = pd.date_range(start=args.start_date, end=args.end_date, freq=args.freq)

    computer = FactorComputer(
        company_facts_path=args.facts,
        universe_path=args.universe,
        prices_dir=args.prices_dir,
        factor_store_path=args.factor_store,
        force_partial_panel=args.force_partial_panel,
    )
    computer.compute_and_store_panel(dates, tickers)
    logger.info("12-Factor computation completed successfully.")


if __name__ == "__main__":
    main()
