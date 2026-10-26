"""
Arm A Composite Score Engine - 7 cross-sectional factors, 65/35 reweight.
(Workstream B, plan v2.0 sec.4)

WHAT THIS IS
------------
A *security-selection* composite.  For a cross-section of tickers it produces
one score per ticker by combining four return-predictive factors (65% mass)
with three operational-health factors (35% mass), exactly as declared in
``config/arm_a_refactor_prereg.yaml``.

WHAT THIS IS NOT
----------------
This is **not** ``ingestion/harness/gate_engine.py``.  That module scores a
*strategy* for the Phase 9 ``composite_dsr`` falsification metric over
concatenated fold returns.  This module scores *tickers*.  They share a module
name and the word "composite" and nothing else; do not merge them, and never
read weights from one for the other.

  ingestion.harness.gate_engine  -> strategy-level, DSR, components
                                    (sharpe, max_drawdown, turnover, hmm_ari,
                                     swap_churn, tca_bps, probation_pass_rate)
  gate_engine (this file)        -> ticker-level, cross-sectional, components
                                    (momentum_12m_1m, eva_spread,
                                     ocf_yield_revision, dividend_carry_coverage,
                                     realized_sharpe, max_drawdown,
                                     transaction_fee_drag)

WHY THE DIRECTION MAP IS EXPLICIT
---------------------------------
Two of the seven factors are penalties whose natural numeric sign is opposite
to their desirability, and a silent sign error here is invisible: the composite
still returns plausible numbers, it just systematically prefers drawdown names
and high-turnover names.  Every factor therefore declares its sign in
:data:`FACTOR_DIRECTION`, and :func:`compute_composite_score` raises if a
configured factor has no declared direction rather than guessing.

  max_drawdown  -> compute_max_drawdown_penalty already returns a NEGATIVE
                   number (e.g. -0.25), so "less negative is better" and no
                   flip is applied.  Flipping it again inverts the risk control.
  fee_drag      -> compute_fee_drag_penalty returns a POSITIVE magnitude (cost
                   paid), so it IS negated for scoring.

ZERO-SYNTHETIC DISCIPLINE
-------------------------
v7's ``optimization.synthetic_fallback: "disabled"`` governs here.  The CESI
incident that produced v7 was exactly this failure: a named factor filled with
``np.random.normal`` so the pipeline would run.  Consequently every factor here
either computes from real inputs or raises :class:`FactorDataUnavailable`.  No
factor returns 0.0 to mean "I have no data" - 0.0 is a legitimate
cross-sectional z-score (it means "average"), so substituting it for missing
data would quietly pull a name toward the middle of the ranking.

Zero-synthetic also means no forward-filled fundamentals: filings are joined
on ``filed_date <= as_of`` only.

CONSEQUENCE FOR TODAY'S RUN
---------------------------
Three of the seven factors lack a complete input path in this repo (see
``factor_data_contracts`` in the registry):

  ocf_yield_revision        (0.15) NOT computable - no market cap and no EPS
                                   estimates in the PIT fundamentals table
  eva_spread                (0.15) PARTIAL     - no PIT sector map to
                                   sector-neutralise against
  dividend_carry_coverage   (0.15) PARTIAL     - dividends are per-share while
                                   FCFE is absolute; no PIT share count to
                                   bridge the units

:func:`compute_composite_score` raises :class:`InsufficientFactorCoverage` when
the computable weight falls below ``min_weight_coverage`` rather than
renormalising the survivors.  Renormalising would silently re-weight the
strategy after the fact, which is the specific thing a pre-registration exists
to prevent.  Coverage-adjusted weights are a CEO ruling and a registry edit,
recorded - not a runtime branch.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from config import (
    ARM_A_CONFIG_PATH,
    get_composite_config,
    get_cost_config,
    get_factor_data_contracts,
)

__all__ = [
    "FactorDataUnavailable",
    "InsufficientFactorCoverage",
    "FACTOR_DIRECTION",
    "TRADING_DAYS",
    "load_composite_config",
    "compute_momentum_12m_1m",
    "compute_eva_spread",
    "compute_ocf_yield_revision",
    "compute_dividend_carry_coverage",
    "compute_realized_sharpe",
    "compute_max_drawdown_penalty",
    "compute_fee_drag_penalty",
    "compute_composite_score",
    "score_breakdown",
    "rank_zscore",
    "cross_sectional_zscore",
]

#: Annualisation factor for daily returns.  Matches the repo's existing
#: convention in portfolio/allocator.py (_TRADING_DAYS) and in
#: ingestion.harness.gate_engine._annualization(), which infers 252 for daily
#: and 12 for monthly series.
TRADING_DAYS = 252

#: +1 = higher raw value scores higher; -1 = lower raw value scores higher.
#: See module docstring.  Every configured factor MUST appear here.
FACTOR_DIRECTION: Dict[str, int] = {
    "momentum_12m_1m": +1,
    "eva_spread": +1,
    "ocf_yield_revision": +1,
    "dividend_carry_coverage": +1,
    "realized_sharpe": +1,
    "max_drawdown": +1,          # already negative; do NOT negate
    "transaction_fee_drag": -1,  # positive cost magnitude; negate for scoring
}

#: Factors whose cross-section is derived from a returns panel rather than from
#: the fundamentals table.  Kept explicit so compute_composite_score can report
#: exactly which inputs each block needed.
_PRICE_FACTORS = ("momentum_12m_1m",)
_FUNDAMENTAL_FACTORS = ("eva_spread", "ocf_yield_revision",
                        "dividend_carry_coverage")
_OPERATIONAL_FACTORS = ("realized_sharpe", "max_drawdown",
                        "transaction_fee_drag")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class FactorDataUnavailable(RuntimeError):
    """
    A factor's declared inputs are absent.

    Raised instead of returning zeros, NaNs or synthetic values.  ``factor``
    names the factor; ``missing`` lists what was absent.
    """

    def __init__(self, factor: str, missing: Sequence[str],
                 detail: str = "") -> None:
        msg = (f"factor {factor!r} cannot be computed: missing {list(missing)}"
               + (f" - {detail}" if detail else "")
               + ". Zero-synthetic policy: returning 0.0/NaN here would be "
                 "indistinguishable from a genuine average z-score and would "
                 "silently pull names toward the middle of the ranking.")
        super().__init__(msg)
        self.factor = factor
        self.missing = list(missing)


class InsufficientFactorCoverage(RuntimeError):
    """
    Too little of the composite's declared weight is computable.

    Carries per-factor detail so the caller sees exactly which input pipeline
    is blocking, rather than only that "something" was unavailable.
    """

    def __init__(self, computable_weight: float, required: float,
                 unavailable: Mapping[str, str]) -> None:
        super().__init__(
            f"only {computable_weight:.4f} of the {required:.4f} declared "
            f"composite weight is computable (required minimum "
            f"{required:.4f}). Unavailable: {dict(unavailable)}. "
            f"This engine does NOT renormalise the surviving weights - doing "
            f"so silently re-weights the strategy after the fact.")
        self.computable_weight = computable_weight
        self.required = required
        self.unavailable = dict(unavailable)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def rank_zscore(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """
    ``zscore(rank(x))`` - the plan's standard cross-sectional transform.

    Ranking before standardising makes a factor robust to the fat tails and
    scale differences that make raw OCF yields and raw EVA spreads incomparable
    across sectors; z-scoring the ranks then puts every factor on a common
    scale, so the weighted sum is a genuine average rather than a contest
    decided by whichever factor happens to have the widest numeric range.

    Ties share the average rank, so a factor with many identical values (a
    capped coverage ratio, for instance) does not manufacture dispersion that
    is not in the data.
    """
    v = pd.Series(values, dtype=float)
    valid = v.dropna()
    out = pd.Series(np.nan, index=v.index, dtype=float)
    if len(valid) < 2:
        # A cross-section of one has no dispersion; z-score is undefined, and
        # undefined is not zero.
        return out
    ranks = valid.rank(method="average", ascending=not higher_is_better)
    sd = ranks.std(ddof=1)
    if not np.isfinite(sd) or sd == 0.0:
        return out
    out.loc[valid.index] = (ranks - ranks.mean()) / sd
    return out


def cross_sectional_zscore(values: pd.Series,
                           higher_is_better: bool = True) -> pd.Series:
    """
    Z-score a cross-section directly, without ranking.

    Used by :func:`compute_eva_spread` *within* each sector, where de-meaning
    must happen before the global rank.  Ranking globally first would destroy
    the within-sector contrast that neutralisation exists to create.
    """
    v = pd.Series(values, dtype=float)
    valid = v.dropna()
    out = pd.Series(np.nan, index=v.index, dtype=float)
    if len(valid) < 2:
        return out
    sd = valid.std(ddof=1)
    if not np.isfinite(sd) or sd == 0.0:
        # Zero dispersion: every name ties, so there is no information.
        return out
    z = (valid - valid.mean()) / sd
    out.loc[valid.index] = z if higher_is_better else -z
    return out


def _resolve_column(frame: pd.DataFrame, candidates: Sequence[str],
                    factor: str) -> str:
    """
    Return the first candidate column actually present in ``frame``.

    Column naming drifted between pipeline generations (``wacc`` vs
    ``wacc_estimate``), so factors accept an ordered candidate list rather than
    one frozen name.  Resolution is explicit and reported; it never guesses
    from whatever happens to be in the frame.
    """
    for col in candidates:
        if col in frame.columns:
            return col
    raise FactorDataUnavailable(
        factor, list(candidates),
        f"none of the candidate columns exist; frame has "
        f"{sorted(map(str, frame.columns))[:25]}")


def _as_ticker_indexed(frame: pd.DataFrame, factor: str,
                       required: Sequence[str]) -> pd.DataFrame:
    """
    Normalise a fundamentals frame to a ticker-indexed frame.

    Handles both layouts present in this repo: a ``ticker`` column, or an
    already ticker-indexed frame.  Duplicate tickers raise rather than silently
    keeping the last, because a duplicated PIT row means the join key is wrong
    and keep-last would select an arbitrary filing.
    """
    if frame is None or len(frame) == 0:
        raise FactorDataUnavailable(factor, list(required), "empty frame")
    f = frame.copy()
    if "ticker" in f.columns:
        f = f.set_index("ticker")
    f.index = pd.Index([str(i).upper() for i in f.index], name="ticker")
    if f.index.has_duplicates:
        dupes = sorted(set(f.index[f.index.duplicated()]))[:10]
        raise FactorDataUnavailable(
            factor, ["unique ticker index"],
            f"duplicated tickers {dupes}; a duplicated PIT row means the join "
            f"key is ambiguous and picking one would be arbitrary")
    missing = [c for c in required if c not in f.columns]
    if missing:
        raise FactorDataUnavailable(
            factor, missing, f"frame has {sorted(map(str, f.columns))[:25]}")
    return f


def _pit_asof(fundamentals: pd.DataFrame, as_of: Optional[pd.Timestamp],
              factor: str) -> pd.DataFrame:
    """
    Reduce a PIT fundamentals table to one filing per ticker as of ``as_of``.

    Point-in-time rule: only rows whose ``filed_date`` is on or before ``as_of``
    may be used; among those the latest ``filed_date`` wins.  No forward fill and
    no bfill.  An as-of join that leaks a later filing is lookahead, and
    lookahead is the one bug that makes a backtest meaningless while still
    producing a beautiful equity curve.
    """
    if as_of is None or "filed_date" not in fundamentals.columns:
        return fundamentals
    f = fundamentals.copy()
    f["filed_date"] = pd.to_datetime(f["filed_date"], errors="coerce")
    # A row whose filed_date will not parse cannot be shown to be
    # PIT-compliant, so it cannot be used to claim compliance either.
    f = f[f["filed_date"].notna()]
    f = f[f["filed_date"] <= pd.Timestamp(as_of)]
    if f.empty:
        raise FactorDataUnavailable(
            factor, ["filed_date <= as_of"],
            f"no filing available on or before {pd.Timestamp(as_of).date()}")
    f = f.sort_values(["ticker", "filed_date"])
    return f.groupby("ticker", as_index=False).last()


def _normalise_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Coerce a price input to a wide ``date x ticker`` float frame.

    Accepts the wide panel that ``data/master_prices`` assembles into, or a
    long ``date x ticker`` MultiIndex frame for convenience.
    """
    if prices is None or len(prices) == 0:
        raise ValueError("prices is empty")
    p = prices.copy()
    if isinstance(p.index, pd.MultiIndex):
        p = p.unstack(level=-1)
    if not isinstance(p.index, pd.DatetimeIndex):
        try:
            p.index = pd.to_datetime(p.index)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"prices index is not a DatetimeIndex and could not be parsed: "
                f"{type(p.index).__name__}") from exc
    p.index = pd.DatetimeIndex(p.index)
    p = p.sort_index()
    p.columns = pd.Index([str(c).upper() for c in p.columns], name="ticker")
    return p.astype(float)


def _returns_panel(prices: pd.DataFrame) -> pd.DataFrame:
    """Wide ``date x ticker`` simple returns from a wide close panel."""
    p = _normalise_prices(prices)
    return p.pct_change()


def load_composite_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    Load the composite weights block from the Arm A registry.

    Args:
        config_path: registry path.  Defaults to
            :data:`config.ARM_A_CONFIG_PATH`
            (``config/arm_a_refactor_prereg.yaml``).

    Returns:
        ``{"directional_alpha": {"total_weight": .., "components": {..}},
           "operational_health": {...}}``

    Raises:
        config.ConfigKeyError: the registry has no ``composite_weights``
            section.  This is exactly what you get if you point this at the
            frozen Phase 9 v7 pre-registration, which describes the
            ``composite_dsr`` *strategy* metric and has no such section.  The
            error is raised deliberately rather than defaulted so the two
            registries cannot be silently confused for one another.
        config.ConfigError: a block weight disagrees with its components.
    """
    return get_composite_config(config_path)


# ---------------------------------------------------------------------------
# Directional alpha factors (0.65 block)
# ---------------------------------------------------------------------------

def compute_momentum_12m_1m(
        prices: pd.DataFrame,
        lookback_12m: int = TRADING_DAYS,
        lookback_1m: int = 21) -> Union[pd.Series, pd.DataFrame]:
    """
    Cross-sectional 12-month minus 1-month momentum, ``zscore(rank(.))``.

    The skipped most-recent month is the point of the factor: short-horizon
    reversal contaminates a plain 12-month return, so the classic construction
    subtracts the last month to measure momentum net of that reversal.

    Built from price *ratios* rather than compounded daily returns so the
    figure is invariant to share-price scale.

    Args:
        prices: wide ``date x ticker`` close panel (cf.
            ``data/master_prices/{ticker}.parquet``, column ``Close``).
        lookback_12m: trading days in the long leg.
        lookback_1m: trading days in the skipped recent leg.

    Returns:
        ``DataFrame`` (date x ticker) of z-scored ranks when ``prices`` spans
        more than one date; a single-date ``Series`` indexed by ticker
        otherwise.  The plan's ``-> pd.Series`` annotation describes the
        single-date cross-section, but "z-scored ranks per date" requires a
        panel, so the return type follows the input.

    NaN policy: names lacking enough history are NaN, never 0.
    """
    p = _normalise_prices(prices)
    if lookback_12m <= lookback_1m:
        raise ValueError(
            f"lookback_12m ({lookback_12m}) must exceed lookback_1m "
            f"({lookback_1m})")

    # ret(L) = P[t] / P[t-L] - 1, so the recent `lookback_1m` days are skipped.
    raw = (p / p.shift(lookback_12m) - 1.0) - (p / p.shift(lookback_1m) - 1.0)

    out = raw.copy().astype(float)
    for dt in out.index:
        out.loc[dt] = rank_zscore(out.loc[dt], higher_is_better=True)
    return out.iloc[-1] if len(out.index) == 1 else out


def compute_eva_spread(fundamentals: pd.DataFrame,
                       as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    """
    Economic Value Added spread = ``ROIC - WACC``, sector-neutralised.

    EVA spread is the return a firm earns above its cost of capital: a
    persistently wide spread means the business converts capital into profit
    more efficiently than its risk requires.  De-meaning within sector removes
    the structural level difference between, say, Utilities and Software, so
    the factor measures *relative* quality rather than sector membership.

    Args:
        fundamentals: PIT table needing a ticker key, a ROIC column, a WACC
            column and a sector column.  In this repo the WACC column is named
            ``wacc_estimate`` rather than ``wacc``; both are accepted.
        as_of: point-in-time cutoff applied on ``filed_date``.

    Returns:
        ``Series`` of z-scored, sector-neutral ranks indexed by ticker.

    Raises:
        FactorDataUnavailable: no sector column.  This is NOT optional and is
            not defaulted to "no neutralisation" - an un-neutralised EVA spread
            is a *different factor* that structurally favours low-WACC sectors,
            and running it under this factor's name would misattribute a sector
            tilt to stock selection.  On current repo data this always raises;
            see ``factor_data_contracts`` in the registry.
    """
    f = _pit_asof(fundamentals, as_of, "eva_spread")
    roic_col = _resolve_column(f, ["roic"], "eva_spread")
    wacc_col = _resolve_column(
        f, ["wacc_estimate", "wacc", "wacc_pct"], "eva_spread")
    sector_col = _resolve_column(
        f, ["sector", "gics_sector", "gics_sector_name", "gics_industry",
            "industry"], "eva_spread")

    f = _as_ticker_indexed(f, "eva_spread", [roic_col, wacc_col, sector_col])
    spread = (pd.to_numeric(f[roic_col], errors="coerce")
              - pd.to_numeric(f[wacc_col], errors="coerce"))
    sector = f[sector_col].astype(str)

    valid = spread.notna() & (sector != "") & (sector.str.lower() != "nan")
    if not valid.any():
        raise FactorDataUnavailable(
            "eva_spread", ["non-null roic/wacc/sector"],
            "every row lost its ROIC, WACC or sector")

    # Neutralise FIRST (within-sector z-score), then rank globally.  Reversing
    # these two steps lets the global rank erase the within-sector contrast that
    # is the entire reason for neutralising.
    neutral = pd.Series(np.nan, index=spread.index, dtype=float)
    for sec in sorted(set(sector[valid])):
        names = sector.index[(sector == sec) & valid]
        if len(names) < 2:
            # A one-name sector cannot be de-meaned; leave NaN rather than
            # inventing a z-score of 0.
            continue
        neutral.loc[names] = cross_sectional_zscore(spread.loc[names])

    return rank_zscore(neutral, higher_is_better=True)


def compute_ocf_yield_revision(
        fundamentals: pd.DataFrame,
        prices: pd.DataFrame,
        as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    """
    ``OCF Yield x sign(EPS revision 3m)``.

    Two ideas multiplied.  OCF yield is a cash-generation quality measure (cash
    from operations per dollar of market value).  The revision sign is the
    catalyst: analysts raising estimates is what actually moves prices over the
    next quarter.  Multiplying means a high-quality name with *falling*
    estimates scores badly, which is the intended behaviour - quality on its own
    is already priced.

    Args:
        fundamentals: needs ``operating_cash_flow``, a market-cap basis (a
            ``market_cap`` column, or a share count combined with ``prices``),
            and two analyst EPS snapshots separated by the revision horizon.
        prices: close panel, used only when the cap must be built from shares.
        as_of: PIT cutoff on ``filed_date``.

    Returns:
        ``Series`` of z-scored ranks indexed by ticker.

    Raises:
        FactorDataUnavailable: any declared input absent.  As of 2026-10-04
            this always raises on repo data: ``valuation_fundamentals.parquet``
            carries neither a market cap nor a share count nor any analyst EPS
            estimate, so both the OCF-yield denominator and the revision sign
            are missing.  The factor is declared at 0.15 and is genuinely not
            computable until a consensus feed and a PIT share count land.
    """
    f = _pit_asof(fundamentals, as_of, "ocf_yield_revision")
    ocf_col = _resolve_column(
        f, ["operating_cash_flow", "ocf"], "ocf_yield_revision")

    if any(c in f.columns for c in ("market_cap", "market_capitalization")):
        cap_col = _resolve_column(f, ["market_cap", "market_capitalization"],
                                  "ocf_yield_revision")
        cap = pd.to_numeric(f[cap_col], errors="coerce")
    elif any(c in f.columns for c in ("shares_outstanding", "diluted_shares",
                                      "shares_diluted")):
        share_col = _resolve_column(
            f, ["shares_outstanding", "diluted_shares", "shares_diluted"],
            "ocf_yield_revision")
        if prices is None:
            raise FactorDataUnavailable(
                "ocf_yield_revision", ["prices"],
                "no market_cap column, and a share count needs a price to "
                "build the capitalisation")
        shares = pd.to_numeric(f[share_col], errors="coerce")
        last_close = _normalise_prices(prices).ffill().iloc[-1]
        px = pd.Series({str(i): last_close.get(str(i), np.nan)
                        for i in shares.index}, dtype=float)
        cap = shares * px.reindex(shares.index)
    else:
        raise FactorDataUnavailable(
            "ocf_yield_revision", ["market_cap", "shares_outstanding"],
            "the PIT fundamentals table carries neither a market cap nor a "
            "share count, so OCF yield has no denominator")

    cur = _resolve_column(
        f, ["eps_est_current", "eps_estimate_current", "eps_est",
            "consensus_eps"], "ocf_yield_revision")
    prior = _resolve_column(
        f, ["eps_est_3m_ago", "eps_est_prior", "eps_estimate_prior", "eps_est_3m"],
        "ocf_yield_revision")

    f = _as_ticker_indexed(f, "ocf_yield_revision", [ocf_col])
    cap_s = pd.to_numeric(pd.Series(cap, index=f.index), errors="coerce")
    ocf = pd.to_numeric(f[ocf_col], errors="coerce")
    eps_cur = pd.to_numeric(f[cur], errors="coerce")
    eps_prior = pd.to_numeric(f[prior], errors="coerce")

    denom = eps_prior.abs()
    revision = (eps_cur - eps_prior) / denom.where(denom > 0)
    ocf_yield = ocf / cap_s.where(cap_s > 0)

    return rank_zscore(ocf_yield * np.sign(revision), higher_is_better=True)


def compute_dividend_carry_coverage(
        fundamentals: pd.DataFrame,
        dividends: Optional[pd.DataFrame] = None,
        as_of: Optional[pd.Timestamp] = None,
        coverage_cap: float = 2.0) -> pd.Series:
    """
    ``FCFE / Dividends paid``, capped at 2.0 - carry sustainability.

    Dividend *yield* alone is a yield trap: a collapsing price manufactures a
    spectacular yield.  Coverage fixes that by asking whether the cash flow
    actually pays the dividend.  Above the cap the extra coverage is
    deliberately discarded - a company hoarding cash far beyond its payout has
    better uses for it, so the signal saturates instead of rewarding excessive
    retention.

    Args:
        fundamentals: needs an FCFE proxy.  The repo's PIT table exposes ``fcf``
            (absolute dollars), which is an FCFE *proxy*, not FCFE itself.
        dividends: per-share dividend panel, wide ``date x ticker`` (cf.
            ``data/master_dividends/{ticker}.parquet``, column ``Dividends``).
        as_of: PIT cutoff on ``filed_date``.
        coverage_cap: saturation cap on the coverage ratio.  Stated in the plan
            prose but NOT declared in the registry; kept as an explicit
            parameter default rather than read from config because the registry
            does not declare it.  Flagged for the next ratification.

    Returns:
        ``Series`` of z-scored ranks indexed by ticker.

    Raises:
        FactorDataUnavailable: dividends absent, or no share count to bridge
            units.  Dividends arrive per-share while ``fcf`` is an absolute
            dollar figure, so dividing one by the other is a unit error rather
            than a factor.
    """
    f = _pit_asof(fundamentals, as_of, "dividend_carry_coverage")
    fcfe_col = _resolve_column(
        f, ["fcfe", "fcf", "free_cash_flow"], "dividend_carry_coverage")

    if dividends is None or len(dividends) == 0:
        raise FactorDataUnavailable(
            "dividend_carry_coverage", ["dividends"],
            "no dividend panel supplied; carry coverage is undefined without it")

    if not any(c in f.columns for c in ("shares_outstanding", "diluted_shares",
                                        "shares_diluted")):
        raise FactorDataUnavailable(
            "dividend_carry_coverage", ["shares_outstanding"],
            f"dividends are per-share but {fcfe_col!r} is an absolute dollar "
            f"figure; a PIT share count is required to bridge the units, and "
            f"dividing one by the other would be a unit error")

    share_col = _resolve_column(
        f, ["shares_outstanding", "diluted_shares", "shares_diluted"],
        "dividend_carry_coverage")
    f = _as_ticker_indexed(f, "dividend_carry_coverage", [fcfe_col, share_col])

    fcfe = pd.to_numeric(f[fcfe_col], errors="coerce")
    shares = pd.to_numeric(f[share_col], errors="coerce")

    dv = _normalise_prices(dividends)
    # Trailing 12 months of declared dividends, summed per ticker.  Only
    # payments already made as of the panel end are counted, so the ratio
    # cannot use a dividend the market has not yet seen.
    window = dv.loc[dv.index >= (dv.index.max() - pd.DateOffset(months=12))]
    div_ps = window.sum(axis=0)
    div_ps = pd.Series({str(i): div_ps.get(str(i), np.nan)
                        for i in f.index}, dtype=float)
    div_dollars = div_ps * shares

    denom = div_dollars.abs().where(div_dollars.abs() > 0)
    coverage = (fcfe / denom).clip(lower=0.0, upper=float(coverage_cap))
    # No dividend -> no carry to underwrite -> not a candidate.
    coverage = coverage.where(div_dollars > 0)
    return rank_zscore(coverage, higher_is_better=True)


# ---------------------------------------------------------------------------
# Operational health factors (0.35 block)
#
# These are per-entity scalars in the plan.  compute_composite_score maps them
# across the cross-section via _operational_panel() so they can be combined
# with the cross-sectional factors on one scale.
# ---------------------------------------------------------------------------

def compute_realized_sharpe(returns: pd.Series, window: int = TRADING_DAYS,
                            periods_per_year: int = TRADING_DAYS) -> float:
    """
    Realized Sharpe ratio over the trailing ``window`` observations.

    Excess-return-free (rf = 0), consistent with the repo's existing Sharpe
    conventions.  Annualised by ``periods_per_year``; pass 12 for monthly
    input.

    Returns:
        float.  ``nan`` when the window is too short or has no dispersion -
        a Sharpe over a single observation is undefined, and 0.0 would assert
        "average performance", which is a claim the data does not support.
    """
    r = pd.Series(returns, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 2:
        return float("nan")
    r = r.tail(int(window))
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd == 0.0:
        return float("nan")
    return float(r.mean() / sd * np.sqrt(periods_per_year))


def compute_max_drawdown_penalty(wealth_curve: pd.Series) -> float:
    """
    Worst peak-to-trough decline, returned NEGATIVE.

    ``-max((cummax(w) - w) / cummax(w))`` -> a loss of 25% returns ``-0.25``.

    The negative sign is load-bearing: it makes "closer to zero is better"
    true without a special case, which is why :data:`FACTOR_DIRECTION` marks
    ``max_drawdown`` as ``+1``.  Negating this a second time at scoring time
    would invert the risk control and start preferring the deepest drawdowns.

    Returns:
        float in ``[-1, 0]``.  ``nan`` for an empty or non-positive curve.
    """
    w = pd.Series(wealth_curve, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(w) == 0:
        return float("nan")
    peak = w.cummax()
    if not (peak > 0).any():
        return float("nan")
    dd = ((peak - w) / peak).max()
    if not np.isfinite(dd):
        return float("nan")
    return float(-abs(dd))


def compute_fee_drag_penalty(turnover: float,
                             round_trip_bps: float = 15.0) -> float:
    """
    Annualised cost paid: ``turnover x round_trip_bps``, in basis points.

    Returned as a POSITIVE magnitude because that is the direction of the
    cost, and :data:`FACTOR_DIRECTION` marks ``transaction_fee_drag`` as ``-1``
    so scoring negates it.  Keeping the arithmetic honest (cost is positive)
    and the sign convention in one declared place is what stops the two from
    cancelling out by accident.

    Args:
        turnover: one-way turnover as a fraction (0.10 = 10%).
        round_trip_bps: round-trip cost.  Defaults to the registry's declared
            15 bps; pass the value from :func:`config.get_cost_config` to keep
            the composite bound to the pre-registration.

    Returns:
        float >= 0, in bps.
    """
    t = float(turnover)
    if not np.isfinite(t):
        return float("nan")
    return abs(t) * float(round_trip_bps)


def _operational_panel(
        prices: pd.DataFrame,
        tickers: Sequence[str],
        window: int = TRADING_DAYS,
        turnover_by_ticker: Optional[Mapping[str, float]] = None,
        round_trip_bps: float = 15.0) -> pd.DataFrame:
    """
    Map the three operational scalars across the cross-section.

    Returns a ``ticker x factor`` frame of z-scored ranks, ready to be combined
    with the directional block.  Operational inputs are derived from
    ``prices`` when available:
      realized_sharpe        trailing Sharpe of each ticker's returns
      max_drawdown           worst drawdown of each ticker's buy-and-hold curve
      transaction_fee_drag   from the caller-supplied turnover map

    A ticker with no turnover observation yields NaN for fee drag rather than
    an assumed turnover, because assuming one would hand every name the same
    cost score and quietly flatten that third of the composite.
    """
    p = _normalise_prices(prices)
    cols = [t for t in (str(x).upper() for x in tickers) if t in p.columns]
    out = pd.DataFrame(index=pd.Index([str(t).upper() for t in tickers],
                                      name="ticker"),
                       columns=list(_OPERATIONAL_FACTORS), dtype=float)

    if cols:
        rets = p[cols].pct_change()
        wealth = p[cols] / p[cols].iloc[0]
        for t in cols:
            out.loc[t, "realized_sharpe"] = compute_realized_sharpe(
                rets[t].dropna(), window=window)
            out.loc[t, "max_drawdown"] = compute_max_drawdown_penalty(
                wealth[t].dropna())
        sub = out.loc[cols, ["realized_sharpe", "max_drawdown"]]
        out.loc[cols, ["realized_sharpe", "max_drawdown"]] = sub.apply(
            lambda col: rank_zscore(col, higher_is_better=True))

    if turnover_by_ticker:
        for t, tv in turnover_by_ticker.items():
            key = str(t).upper()
            if key in out.index:
                out.loc[key, "transaction_fee_drag"] = compute_fee_drag_penalty(
                    tv, round_trip_bps=round_trip_bps)
        have = out["transaction_fee_drag"].notna()
        if have.any():
            out.loc[have, "transaction_fee_drag"] = rank_zscore(
                out.loc[have, "transaction_fee_drag"], higher_is_better=False)

    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_composite_score(
        tickers: List[str],
        prices: pd.DataFrame,
        fundamentals: Optional[pd.DataFrame] = None,
        config: Optional[Dict[str, Any]] = None,
        *,
        dividends: Optional[pd.DataFrame] = None,
        returns_panel: Optional[pd.DataFrame] = None,
        turnover_by_ticker: Optional[Mapping[str, float]] = None,
        as_of: Optional[pd.Timestamp] = None,
        config_path: str = ARM_A_CONFIG_PATH,
        min_weight_coverage: float = 0.90,
        strict_directions: bool = True) -> pd.Series:
    """
    MAIN ENTRY POINT.  Composite score per ticker.

    Steps:
      1. Read the 65/35 block weights from config (never from literals here).
      2. Compute each factor over the cross-section.
      3. Z-score each factor cross-sectionally (``zscore(rank(.))``).
      4. Apply the declared direction, then weight and sum.
      5. Return a ``Series`` indexed by ticker.

    Args:
        tickers: the cross-section.  Case-insensitive; output is upper-cased.
        prices: wide ``date x ticker`` close panel.
        fundamentals: PIT fundamentals table.  Optional only because the
            factors that need it are skipped when their inputs are absent;
            in practice passing ``None`` forfeits 45% of the declared weight.
        config: pre-loaded ``composite_weights`` block.  When omitted it is
            read from ``config_path``.
        dividends: per-share dividend panel for carry coverage.
        returns_panel: optional pre-computed returns.  Derived from ``prices``
            when omitted.  Supplied by the caller when the operational factors
            should describe *strategy* returns rather than each ticker's own.
        turnover_by_ticker: per-name turnover for the fee-drag factor.
        as_of: PIT cutoff for fundamentals.
        config_path: registry to read when ``config`` is omitted.
        min_weight_coverage: minimum fraction of declared weight that must be
            computable.  Below this, raise
            :class:`InsufficientFactorCoverage`.  Set to 0.0 to always get a
            score over whatever is available - the returned Series then carries
            only the computable weight, which the caller must not mistake for
            the pre-registered composite.
        strict_directions: raise when a configured factor has no declared
            direction.  Leave on; turning it off means an undeclared factor is
            scored with a guessed sign.

    Returns:
        ``Series`` of composite scores indexed by ticker.

    Raises:
        InsufficientFactorCoverage: computable weight below the minimum.
        FactorDataUnavailable: a factor that was *expected* to be computable
            had an input problem (as opposed to being absent by design).
    """
    idx = pd.Index([str(t).upper() for t in tickers], name="ticker")
    if idx.has_duplicates:
        dupes = sorted(set(idx[idx.duplicated()]))[:10]
        raise ValueError(f"duplicate tickers in the cross-section: {dupes}")

    weights = config if config is not None else load_composite_config(config_path)
    flat: Dict[str, float] = {}
    for block in ("directional_alpha", "operational_health"):
        for name, w in weights[block]["components"].items():
            flat[str(name)] = float(w)

    if strict_directions:
        undeclared = sorted(set(flat) - set(FACTOR_DIRECTION))
        if undeclared:
            raise ValueError(
                f"factors {undeclared} are configured with weights but have no "
                f"entry in FACTOR_DIRECTION. Add the sign explicitly rather "
                f"than letting the composite guess it - a wrong sign is "
                f"invisible in the output and inverts the factor.")

    cost_cfg = get_cost_config(config_path)
    round_trip_bps = float(cost_cfg.get("round_trip_bps", 15))

    parts: Dict[str, pd.Series] = {}
    unavailable: Dict[str, str] = {}

    # -- directional alpha -------------------------------------------------
    try:
        mom = compute_momentum_12m_1m(prices)
        parts["momentum_12m_1m"] = (mom if isinstance(mom, pd.Series)
                                    else mom.iloc[-1])
    except FactorDataUnavailable as exc:
        unavailable["momentum_12m_1m"] = str(exc)

    if fundamentals is None:
        for f in ("eva_spread", "ocf_yield_revision", "dividend_carry_coverage"):
            unavailable[f] = "no fundamentals supplied"
    else:
        try:
            parts["eva_spread"] = compute_eva_spread(fundamentals, as_of=as_of)
        except FactorDataUnavailable as exc:
            unavailable["eva_spread"] = str(exc)
        try:
            parts["ocf_yield_revision"] = compute_ocf_yield_revision(
                fundamentals, prices, as_of=as_of)
        except FactorDataUnavailable as exc:
            unavailable["ocf_yield_revision"] = str(exc)
        try:
            parts["dividend_carry_coverage"] = compute_dividend_carry_coverage(
                fundamentals, dividends, as_of=as_of)
        except FactorDataUnavailable as exc:
            unavailable["dividend_carry_coverage"] = str(exc)

    # -- operational health ------------------------------------------------
    op_prices = prices
    if returns_panel is not None:
        # Operational factors describe returns; when the caller supplies a
        # returns panel we score the operational block off it directly.
        rp = returns_panel.copy()
        rp.columns = pd.Index([str(c).upper() for c in rp.columns], name="ticker")
        op = pd.DataFrame(index=idx, columns=list(_OPERATIONAL_FACTORS),
                          dtype=float)
        for t in idx:
            if t in rp.columns:
                series = rp[t].dropna()
                op.loc[t, "realized_sharpe"] = compute_realized_sharpe(series)
                wealth = (1.0 + series).cumprod()
                op.loc[t, "max_drawdown"] = compute_max_drawdown_penalty(wealth)
        op["realized_sharpe"] = rank_zscore(op["realized_sharpe"])
        op["max_drawdown"] = rank_zscore(op["max_drawdown"])
        if turnover_by_ticker:
            for t, tv in turnover_by_ticker.items():
                key = str(t).upper()
                if key in op.index:
                    op.loc[key, "transaction_fee_drag"] = compute_fee_drag_penalty(
                        tv, round_trip_bps=round_trip_bps)
            have = op["transaction_fee_drag"].notna()
            if have.any():
                op.loc[have, "transaction_fee_drag"] = rank_zscore(
                    op.loc[have, "transaction_fee_drag"], higher_is_better=False)
        for c in _OPERATIONAL_FACTORS:
            parts[c] = op[c]
    else:
        if op_prices is None:
            for f in _OPERATIONAL_FACTORS:
                unavailable[f] = "no prices or returns panel supplied"
        else:
            op = _operational_panel(op_prices, list(idx),
                                    turnover_by_ticker=turnover_by_ticker,
                                    round_trip_bps=round_trip_bps)
            for c in _OPERATIONAL_FACTORS:
                parts[c] = op[c]

    # -- coverage gate -----------------------------------------------------
    computable = sum(w for name, w in flat.items() if name in parts)
    total = sum(flat.values())
    coverage = computable / total if total else 0.0
    if coverage < float(min_weight_coverage):
        raise InsufficientFactorCoverage(computable, total, unavailable)

    # -- weighted sum ------------------------------------------------------
    score = pd.Series(np.nan, index=idx, dtype=float)
    for name, w in flat.items():
        if name not in parts:
            continue
        s = pd.to_numeric(parts[name], errors="coerce").reindex(idx)
        direction = FACTOR_DIRECTION.get(name, +1)
        contrib = (s * float(w)) if direction > 0 else (-s * float(w))
        # Re-attribute only where this factor actually scored, so a missing
        # factor leaves NaN rather than being counted as a zero contribution.
        score = score.add(contrib.where(s.notna()), fill_value=0.0)

    score.name = "composite_score"
    return score


def score_breakdown(
        tickers: List[str],
        prices: pd.DataFrame,
        fundamentals: Optional[pd.DataFrame] = None,
        config: Optional[Dict[str, Any]] = None,
        **kwargs: Any) -> pd.DataFrame:
    """
    Diagnostic view of the composite: per-ticker factor z-scores plus the
    signed weighted contribution of each.

    Use this when a ranking looks wrong.  A single flipped sign or one factor
    dominating the dispersion is visible immediately in the contribution
    columns, and invisible in the summed score.
    """
    idx = pd.Index([str(t).upper() for t in tickers], name="ticker")
    weights = config if config is not None else load_composite_config(
        kwargs.get("config_path", ARM_A_CONFIG_PATH))
    flat: Dict[str, float] = {}
    for block in ("directional_alpha", "operational_health"):
        for name, w in weights[block]["components"].items():
            flat[str(name)] = float(w)

    score = compute_composite_score(tickers, prices, fundamentals, config, **kwargs)

    rows: Dict[str, pd.Series] = {}
    for name in flat:
        contrib = pd.Series(np.nan, index=idx, dtype=float)
        rows[f"{name}__z"] = contrib
    rows["composite_score"] = score
    out = pd.DataFrame(rows)
    out.attrs["weights"] = flat
    out.attrs["directions"] = {k: FACTOR_DIRECTION.get(k) for k in flat}
    return out
