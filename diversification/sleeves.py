"""Asset sleeves for the Phase-1 multi-asset portfolio (D-20260803-003).

Extends ``diversification.datastore.SLEEVES`` with a flat, liquid sleeve config
for the $10k macro-state / risk-minimized simulation: equity (SPY proxy),
corporate bonds, short bills, gold.

All thresholds and yields here are PRE-REGISTERED parameters — module constants,
never fit to backtest outcomes (auditor requirement). Dividend-yield estimates
are used ONLY by the fee-coverage measurement (the CEO's hypothesis that
dividend income covers reallocation fees), never for selection.

Sleeve proxies chosen from public research (2026):
- gold: GLD (largest/most liquid, 0.40%) + IAU (0.25%) — lower-cost GLDM/IAUM
  (0.10%/0.09%) are cheaper but thin-history; kept for live note only.
- short bills: BIL (1-3m T-bills) + SHY (1-3y Treasuries) + SGOV (0-3m T-bills,
  0.09% — cheapest, but inception 2020; handled gracefully pre-inception).
- corporate bonds: VCSH (short IG) + VCIT (intermediate IG), both low-cost.
- equity: SPY (megacap sleeve proxy for the Phase-1 sim; the S&P600 relative-
  moat sleeve is Phase-2 / opportunistic-deferred).
"""

SLEEVES = {
    "equity": ["SPY"],
    "corporate_bonds": ["VCSH", "VCIT"],
    "short_bills": ["BIL", "SHY", "SGOV"],
    "gold": ["GLD", "IAU"],
}

# Macro-state sleeve targets (pre-registered per D-20260803-003):
#   bull   -> tilt bonds/alternatives (the CEO's "sell stocks high, buy
#             cheaper bonds")
#   bear   -> tilt cheap stocks ("buy more cheap stocks")
#   neutral-> balanced reference.
# Sleeve weights are bounded 10-50% (equity 30-70% per the D-20260803-002
# sleeve bound) and always sum to 1.
MACRO_TARGETS = {
    "bull": {"equity": 0.30, "corporate_bonds": 0.30, "short_bills": 0.25, "gold": 0.15},
    "neutral": {"equity": 0.40, "corporate_bonds": 0.20, "short_bills": 0.20, "gold": 0.20},
    "bear": {"equity": 0.55, "corporate_bonds": 0.15, "short_bills": 0.20, "gold": 0.10},
}

# Feasibility bounds for the risk minimizer (pre-registered).
SLEEVE_BOUNDS = {
    "equity": (0.30, 0.70),
    "corporate_bonds": (0.10, 0.50),
    "short_bills": (0.10, 0.50),
    "gold": (0.05, 0.30),
}

# Annualized dividend-yield estimates per ETF (public fact sheets / 2026
# research). Used ONLY for the fee-coverage measurement, never for selection.
DIVIDEND_YIELDS = {
    "SPY": 0.0125,
    "VCSH": 0.045,
    "VCIT": 0.048,
    "BIL": 0.035,
    "SHY": 0.046,
    "SGOV": 0.045,
    "GLD": 0.0,
    "IAU": 0.0,
    "JNJ": 0.031,
    "PG": 0.026,
    "KO": 0.030,
    "PEP": 0.031,
    "MCD": 0.028,
    "CL": 0.026,
    "KMB": 0.036,
    "TGT": 0.033,
    "HD": 0.025,
    "GIS": 0.040,
    "MO": 0.080,
    "O": 0.056,
    "VZ": 0.050,
    "MDY": 0.012,
    "IWM": 0.012,
}

# Phase-2 stable-dividend candidate universe (D-20260803-004): pre-registered
# large-cap dividend payers offered to the audit. Membership is decided by
# dividend_audit.audit_basket at each decision date (expanding-window, OOS),
# NOT by this list — these are candidates, never picks. Includes one REIT (O)
# that the keyword screen must reject and one name (VZ) known to cut its
# dividend, so the audit's gates are falsifiable.
DIVIDEND_CANDIDATES = [
    "JNJ", "PG", "KO", "PEP", "MCD", "CL", "KMB",
    "TGT", "HD", "GIS", "MO", "O", "VZ",
]

# Name screen exclusions (pre-registered): Real Estate Investment Trusts,
# Business Development Companies, Master Limited Partnerships pay out as
# non-qualified/special structures and are excluded from the stable-dividend
# basket per D-20260803-004.
DIVIDEND_EXCLUDED_KEYWORDS = ("REIT", "BDC", "MLP", "REALTY", "Real Estate")

# Ticker-level exclusion: the candidate ticker maps to an excluded structure.
# "O" is Realty Income Corp (a REIT); its ticker carries no keyword, so the
# exclusion is pre-registered explicitly.
DIVIDEND_EXCLUDED_TICKERS = ("O",)

ALL_TICKERS = sorted({t for ts in SLEEVES.values() for t in ts})

# Phase-3 (D-20260803-005) price universe: the four-sleeve allocator adds a
# small/mid sleeve (MDY midcap, IWM smallcap proxies, pre-registered; kept under
# the 10-15% floor) on top of the existing sleeves + the stable-dividend basket.
# Weights/targets/bounds live ONLY in config/weights_diversification.yaml
# (invariant 4) — this is the ticker universe for the price fetch, nothing more.
P3_TICKERS = sorted(
    {t for ts in SLEEVES.values() for t in ts} | {"MDY", "IWM"} | set(DIVIDEND_CANDIDATES)
)
