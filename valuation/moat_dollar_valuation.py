"""
valuation/moat_dollar_valuation.py — Moat Dollar Valuation Engine.

Translates qualitative and alternative moats into dollar fair value ($) adjustments
with variance/covariance volatility scaling and flagged concentration fragility.

Mathematical Model:
    Pi_justified(i) = MAX_JUSTIFIED_PREMIUM * MS_i * D_i * V_i
    iv_moat = iv_baseline * (1.0 + Pi_justified)
    Market_Premium = (current_price - iv_baseline) / iv_baseline
    Delta_alpha = Pi_justified - Market_Premium

Where:
    - MAX_JUSTIFIED_PREMIUM = 0.80 (+80% baseline ceiling)
    - MS_i in [0, 1]: Moat Strength derived from tech/patent traction and backlog growth
    - F_i: Fragility = 0.30 * max(0, (cust_conc - 0.30) / 0.20) + 0.40 * legal + 0.30 * supplier_choke
    - D_i = MS_i / (MS_i + F_i + 1e-4) in [0, 1]: Moat Durability
    - V_i in [0.70, 1.25]: Dynamic variance / covariance volatility scaling factor
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np
import pandas as pd


MAX_JUSTIFIED_PREMIUM: float = 0.80
DURABILITY_EPSILON: float = 1e-4


@dataclass
class MoatValuationOutput:
    """Dataclass holding complete moat dollar valuation results for a single firm."""

    ticker: str
    current_price: float
    iv_baseline: float
    moat_strength: float        # MS_i in [0.0, 1.0]
    fragility: float            # F_i >= 0.0
    durability: float           # D_i in [0.0, 1.0]
    volatility_scaler: float    # V_i in [0.70, 1.25]
    justified_premium: float    # Pi_justified = MAX_JUSTIFIED_PREMIUM * MS_i * D_i * V_i
    market_premium: float       # (current_price - iv_baseline) / iv_baseline
    iv_moat: float              # iv_baseline * (1.0 + Pi_justified)
    alpha_spread: float         # Pi_justified - market_premium
    customer_conc_flag: bool    # True if customer_conc_share > 0.30
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert valuation output to a dictionary."""
        return asdict(self)


class MoatDollarValuationEngine:
    """
    Moat Dollar Valuation Engine.

    Translates sector-native traction and ASC 606 backlog into justified dollar premiums
    above baseline DCF fair value, modulated by cash flow variance and market covariance.
    """

    MAX_JUSTIFIED_PREMIUM: float = 0.80

    def __init__(
        self,
        max_justified_premium: float = 0.80,
        median_cf_variance: float = 0.04,
        mad_cf_variance: float = 0.02,
        median_market_covariance: float = 0.02,
        mad_market_covariance: float = 0.01,
    ):
        """
        Initialize the Moat Dollar Valuation Engine with baseline and reference parameters.

        Args:
            max_justified_premium: Baseline ceiling cap for moat premium (default: 0.80, +80%).
            median_cf_variance: Cross-sectional median cash flow variance reference.
            mad_cf_variance: Median Absolute Deviation (MAD) of cash flow variance.
            median_market_covariance: Cross-sectional median equity/market covariance.
            mad_market_covariance: Median Absolute Deviation (MAD) of market covariance.
        """
        self.max_justified_premium = float(max_justified_premium)
        self.median_cf_variance = float(median_cf_variance)
        self.mad_cf_variance = max(float(mad_cf_variance), 1e-6)
        self.median_market_covariance = float(median_market_covariance)
        self.mad_market_covariance = max(float(mad_market_covariance), 1e-6)

    def compute_moat_strength(
        self,
        dev_lockin_z: float,
        ai_dominance_z: float,
        patent_utility_z: float,
        rpo_backlog_z: float,
    ) -> float:
        """
        Compute Moat Strength MS_i in [0.0, 1.0].

        Combines developer lock-in, AI ecosystem dominance, patent utility, and ASC 606 RPO backlog.
        Applies tanh(z / 2.0) smooth bounding to raw z-scores.
        """
        # Bounded driver traction in [-1.0, 1.0]
        z_dev = np.tanh(float(dev_lockin_z) / 2.0)
        z_ai = np.tanh(float(ai_dominance_z) / 2.0)
        z_patent = np.tanh(float(patent_utility_z) / 2.0)
        z_rpo = np.tanh(float(rpo_backlog_z) / 2.0)

        # Tech / Software traction composite T_i
        t_i = 0.35 * z_dev + 0.35 * z_ai + 0.30 * z_patent

        # Composite moat signal including ASC 606 contract backlog growth
        ms_raw = 0.70 * t_i + 0.30 * z_rpo

        # Map [-1.0, 1.0] to [0.0, 1.0]
        ms_i = float(np.clip((1.0 + ms_raw) / 2.0, 0.0, 1.0))
        return ms_i

    def compute_fragility(
        self,
        customer_conc_share: float,
        antitrust_litigation: Union[float, bool],
        supplier_choke_share: float,
    ) -> Tuple[float, bool]:
        """
        Compute Fragility F_i >= 0.0 and customer concentration flag.

        Formula:
            conc_penalty = 0.30 * max(0.0, (cust_conc - 0.30) / 0.20)
            F_i = conc_penalty + 0.40 * antitrust_litigation + 0.30 * supplier_choke_share

        Customer concentration > 30% is flagged gracefully: a modest penalty is applied
        rather than declaring fatal position demise.
        """
        c_share = float(customer_conc_share)
        if c_share > 1.0:
            c_share /= 100.0  # Normalize percentage to decimal

        s_share = float(supplier_choke_share)
        if s_share > 1.0:
            s_share /= 100.0

        if isinstance(antitrust_litigation, bool):
            a_legal = 1.0 if antitrust_litigation else 0.0
        else:
            a_legal = float(np.clip(antitrust_litigation, 0.0, 1.0))

        # Flagged customer concentration penalty
        customer_conc_flag = c_share > 0.30
        if customer_conc_flag:
            conc_penalty = 0.30 * ((c_share - 0.30) / 0.20)
        else:
            conc_penalty = 0.0

        f_i = float(conc_penalty + 0.40 * a_legal + 0.30 * s_share)
        f_i = max(0.0, f_i)
        return f_i, customer_conc_flag

    def compute_durability(
        self,
        moat_strength: float,
        fragility: float,
        epsilon: float = DURABILITY_EPSILON,
    ) -> float:
        """
        Compute Durability D_i in [0.0, 1.0].

        Formula:
            D_i = MS_i / (MS_i + F_i + epsilon)
        """
        ms = float(np.clip(moat_strength, 0.0, 1.0))
        f = max(0.0, float(fragility))
        d_i = ms / (ms + f + epsilon)
        return float(np.clip(d_i, 0.0, 1.0))

    def compute_volatility_scaler(
        self,
        cash_flow_variance: float,
        market_covariance: float,
        median_cf_variance: Optional[float] = None,
        mad_cf_variance: Optional[float] = None,
        median_market_covariance: Optional[float] = None,
        mad_market_covariance: Optional[float] = None,
    ) -> float:
        """
        Compute Dynamic Volatility Scaling Factor V_i in [0.70, 1.25].

        Formula:
            V_i = 1.0 - 0.25 * tanh((var - med_var) / mad_var)
                      - 0.15 * tanh((cov - med_cov) / mad_cov)
            Clipped to [0.70, 1.25].

        Behavior:
            - Scales up towards 1.25 (+100% to +120% moat expansion) when cash flow variance
              and market covariance are low.
            - Scales down towards 0.70 (dampening premium to <= +40%) when volatility and
              covariance are high.
        """
        med_var = float(median_cf_variance if median_cf_variance is not None else self.median_cf_variance)
        mad_var = max(float(mad_cf_variance if mad_cf_variance is not None else self.mad_cf_variance), 1e-6)
        med_cov = float(median_market_covariance if median_market_covariance is not None else self.median_market_covariance)
        mad_cov = max(float(mad_market_covariance if mad_market_covariance is not None else self.mad_market_covariance), 1e-6)

        var_term = np.tanh((float(cash_flow_variance) - med_var) / mad_var)
        cov_term = np.tanh((float(market_covariance) - med_cov) / mad_cov)

        v_i = 1.0 - 0.25 * var_term - 0.15 * cov_term
        return float(np.clip(v_i, 0.70, 1.25))

    def evaluate(
        self,
        ticker: str,
        current_price: float,
        iv_baseline: float,
        dev_lockin_z: float,
        ai_dominance_z: float,
        patent_utility_z: float,
        rpo_backlog_z: float,
        customer_conc_share: float,
        antitrust_litigation: Union[float, bool],
        supplier_choke_share: float,
        cash_flow_variance: float,
        market_covariance: float,
        median_cf_variance: Optional[float] = None,
        mad_cf_variance: Optional[float] = None,
        median_market_covariance: Optional[float] = None,
        mad_market_covariance: Optional[float] = None,
        **kwargs: Any,
    ) -> MoatValuationOutput:
        """
        Evaluate complete Moat Dollar Valuation for a given ticker.

        Returns:
            MoatValuationOutput containing MS_i, F_i, D_i, V_i, Pi_justified, iv_moat, and Delta_alpha.
        """
        p_curr = float(current_price)
        iv_base = float(iv_baseline)

        # 1. Moat Strength MS_i in [0, 1]
        ms_i = self.compute_moat_strength(
            dev_lockin_z=dev_lockin_z,
            ai_dominance_z=ai_dominance_z,
            patent_utility_z=patent_utility_z,
            rpo_backlog_z=rpo_backlog_z,
        )

        # 2. Fragility F_i and customer concentration flag
        f_i, conc_flag = self.compute_fragility(
            customer_conc_share=customer_conc_share,
            antitrust_litigation=antitrust_litigation,
            supplier_choke_share=supplier_choke_share,
        )

        # 3. Durability D_i in [0, 1]
        d_i = self.compute_durability(moat_strength=ms_i, fragility=f_i)

        # 4. Volatility Scaling Factor V_i in [0.70, 1.25]
        v_i = self.compute_volatility_scaler(
            cash_flow_variance=cash_flow_variance,
            market_covariance=market_covariance,
            median_cf_variance=median_cf_variance,
            mad_cf_variance=mad_cf_variance,
            median_market_covariance=median_market_covariance,
            mad_market_covariance=mad_market_covariance,
        )

        # 5. Justified Moat Premium Pi_justified
        pi_justified = float(self.max_justified_premium * ms_i * d_i * v_i)

        # 6. Dollar Fair Value IV_moat
        iv_moat = float(iv_base * (1.0 + pi_justified)) if iv_base > 0.0 else 0.0

        # 7. Market Premium and Alpha Spread
        if iv_base > 0.0:
            market_premium = float((p_curr - iv_base) / iv_base)
        else:
            market_premium = 0.0

        alpha_spread = float(pi_justified - market_premium)

        meta = {
            "max_justified_premium_cap": self.max_justified_premium,
            **kwargs,
        }

        return MoatValuationOutput(
            ticker=str(ticker),
            current_price=p_curr,
            iv_baseline=iv_base,
            moat_strength=ms_i,
            fragility=f_i,
            durability=d_i,
            volatility_scaler=v_i,
            justified_premium=pi_justified,
            market_premium=market_premium,
            iv_moat=iv_moat,
            alpha_spread=alpha_spread,
            customer_conc_flag=conc_flag,
            metadata=meta,
        )

    def compute_valuation(
        self,
        ticker: str,
        current_price: float,
        iv_baseline: float,
        dev_lockin_z: float,
        ai_dominance_z: float,
        patent_utility_z: float,
        rpo_backlog_z: float,
        customer_conc_share: float,
        antitrust_litigation: Union[float, bool],
        supplier_choke_share: float,
        cash_flow_variance: float,
        market_covariance: float,
        **kwargs: Any,
    ) -> MoatValuationOutput:
        """Alias for evaluate."""
        return self.evaluate(
            ticker=ticker,
            current_price=current_price,
            iv_baseline=iv_baseline,
            dev_lockin_z=dev_lockin_z,
            ai_dominance_z=ai_dominance_z,
            patent_utility_z=patent_utility_z,
            rpo_backlog_z=rpo_backlog_z,
            customer_conc_share=customer_conc_share,
            antitrust_litigation=antitrust_litigation,
            supplier_choke_share=supplier_choke_share,
            cash_flow_variance=cash_flow_variance,
            market_covariance=market_covariance,
            **kwargs,
        )

    def evaluate_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Evaluate moat dollar valuation across a pandas DataFrame.

        Expected columns matching the evaluate arguments:
            ['ticker', 'current_price', 'iv_baseline', 'dev_lockin_z', 'ai_dominance_z',
             'patent_utility_z', 'rpo_backlog_z', 'customer_conc_share',
             'antitrust_litigation', 'supplier_choke_share', 'cash_flow_variance',
             'market_covariance']
        """
        # If multiple rows, compute cross-sectional median and MAD for variance & covariance
        med_var = float(df['cash_flow_variance'].median()) if 'cash_flow_variance' in df.columns else None
        mad_var = float((df['cash_flow_variance'] - med_var).abs().median()) if med_var is not None else None
        med_cov = float(df['market_covariance'].median()) if 'market_covariance' in df.columns else None
        mad_cov = float((df['market_covariance'] - med_cov).abs().median()) if med_cov is not None else None

        records = []
        for _, row in df.iterrows():
            res = self.evaluate(
                ticker=row['ticker'],
                current_price=row['current_price'],
                iv_baseline=row['iv_baseline'],
                dev_lockin_z=row.get('dev_lockin_z', 0.0),
                ai_dominance_z=row.get('ai_dominance_z', 0.0),
                patent_utility_z=row.get('patent_utility_z', 0.0),
                rpo_backlog_z=row.get('rpo_backlog_z', 0.0),
                customer_conc_share=row.get('customer_conc_share', 0.0),
                antitrust_litigation=row.get('antitrust_litigation', 0.0),
                supplier_choke_share=row.get('supplier_choke_share', 0.0),
                cash_flow_variance=row.get('cash_flow_variance', 0.04),
                market_covariance=row.get('market_covariance', 0.02),
                median_cf_variance=med_var,
                mad_cf_variance=mad_var,
                median_market_covariance=med_cov,
                mad_market_covariance=mad_cov,
            )
            records.append(res.to_dict())

        return pd.DataFrame(records)


def compute_moat_valuation(
    ticker: str,
    current_price: float,
    iv_baseline: float,
    dev_lockin_z: float,
    ai_dominance_z: float,
    patent_utility_z: float,
    rpo_backlog_z: float,
    customer_conc_share: float,
    antitrust_litigation: Union[float, bool],
    supplier_choke_share: float,
    cash_flow_variance: float,
    market_covariance: float,
    **kwargs: Any,
) -> MoatValuationOutput:
    """Convenience functional interface for MoatDollarValuationEngine."""
    engine = MoatDollarValuationEngine()
    return engine.evaluate(
        ticker=ticker,
        current_price=current_price,
        iv_baseline=iv_baseline,
        dev_lockin_z=dev_lockin_z,
        ai_dominance_z=ai_dominance_z,
        patent_utility_z=patent_utility_z,
        rpo_backlog_z=rpo_backlog_z,
        customer_conc_share=customer_conc_share,
        antitrust_litigation=antitrust_litigation,
        supplier_choke_share=supplier_choke_share,
        cash_flow_variance=cash_flow_variance,
        market_covariance=market_covariance,
        **kwargs,
    )
