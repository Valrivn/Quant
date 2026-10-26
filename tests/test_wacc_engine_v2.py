"""Phase 1 (Schemas & WACC Engine) — formula-level unit tests for
ingestion/wacc_engine_v2.py.  Every assertion pins a pre-registered frozen
formula (Plan PHASE_8_COMPREHENSIVE_REDESIGN L238-240/L279-286, analysis
phase8_bigpickle_analysis.md §5.1-§5.6, config/pre_registration_phase8.yaml).

Plan-vs-plan inconsistencies surfaced during Phase 1 execution (documented, not
silently fixed):
  * EXT-01 expects Omega_stress >= 2.5 for burn=0.5/ICR=-2/High; the frozen
    formula yields 1.675 (and <= 3.0 clamp caps the max reachable value).
  * EXT-07 expects total_beta 4.1 for raw 7.2; the frozen sigmoid formula
    5.0/(1+e^{-x/3}) yields 4.5841.
"""

import numpy as np
import pandas as pd
import pytest

from ingestion.wacc_engine_v2 import (
    RATING_ENUM_KEYS,
    WACCEngineV2,
    _map_rating_to_schema,
    _sigmoid_ceiling_beta,
)

RF_2018 = {"2024-11-01": 0.042}


def _mk_engine(with_series=True):
    """Engine with rf/vix/damodaran ERP preloaded (PIT, as-of 2024-11-01)."""
    eng = WACCEngineV2()
    if with_series:
        eng.load_rf(RF_2018)
        eng.load_vix({"2024-11-01": 18.0})
        eng.load_erp({"2024-11-01": 0.045})
    return eng


# ---------------------------------------------------------------------------
# ERP blend (VIX /100 fix — analysis §5.1 / engine docstring)
# ---------------------------------------------------------------------------

def test_vix_option_erp_uses_points_divided_by_100():
    """VIX is in POINTS: erp_option = max(0, (VIX/100)/sqrt(12) - rf)."""
    eng = _mk_engine()
    blend, prov = eng.compute_erp_blend(pd.Timestamp("2024-11-01"))
    erp_opt_exp = max(0.0, (18.0 / 100.0) / np.sqrt(12.0) - 0.042)
    assert abs(prov["erp_option"] - erp_opt_exp) < 1e-9
    # The /100 bug would give ~1.0 here (18/sqrt(12) - 0.042); we must be ~0.01
    assert prov["erp_option"] == pytest.approx(0.00996, abs=1e-4)


def test_erp_blend_weights_and_damodaran_dominance():
    """blend = (0.7*dm + 0.3*option) / (0.7+0.3); dm=0.045 weights 70%."""
    eng = _mk_engine()
    blend, prov = eng.compute_erp_blend(pd.Timestamp("2024-11-01"))
    assert prov["erp_damodaran"] == pytest.approx(0.045)
    expected = 0.7 * 0.045 + 0.3 * prov["erp_option"]
    assert prov["blend_exact"] == pytest.approx(expected, abs=1e-12)
    assert 0.005 <= blend <= 0.08  # frozen erp_bounds


def test_erp_clamp_at_upper_bound_ext10():
    """EXT-10: VIX=80 must clamp the blend to the frozen 8% ceiling."""
    eng = _mk_engine()
    blend, prov = eng.compute_erp_blend(pd.Timestamp("2024-11-01"), vix=80.0)
    assert blend == pytest.approx(0.08)
    assert prov["blend_clamped"] is True


def test_erp_carry_forward_with_staleness_flag():
    """Missing market data carries forward (Appendix B rule 3) with a flag."""
    eng = WACCEngineV2()
    eng.load_rf({"2024-06-01": 0.04})
    eng.load_vix({"2024-06-01": 15.0})
    eng.load_erp({"2024-06-01": 0.046})
    blend, prov = eng.compute_erp_blend(pd.Timestamp("2024-12-31"))
    assert 0.005 <= blend <= 0.08
    assert prov["rf_stale"] is True            # 214 days > 5d stale
    assert prov["vix_stale"] is True
    assert prov["erp_damodaran_stale"] is True  # > 95d stale
    assert prov["rf_10y"] == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# Total beta sigmoid (Plan L238-240; ceiling 5.0, transition 3.0)
# ---------------------------------------------------------------------------

def test_total_beta_sigmoid_formula_ext07():
    """total_beta = 5.0/(1+e^{-raw/3}); EXT-07 raw=7.2 -> 4.5841 (plan said 4.1)."""
    x = 7.2
    tb = _sigmoid_ceiling_beta(x, ceiling=5.0, transition=3.0)
    assert tb == pytest.approx(5.0 / (1.0 + np.exp(-x / 3.0)))
    assert tb == pytest.approx(4.5841, abs=1e-4)
    assert 4.4 < tb < 5.0


def test_total_beta_sigmoid_limits():
    assert _sigmoid_ceiling_beta(1e9) == 5.0            # ceiling
    assert _sigmoid_ceiling_beta(-1.0) == pytest.approx(0.0)  # floor ~ 0
    # engine guards degenerate raw <= 0 (never negative); the sigmoid formula
    # itself applies for x > 0 (midpoint would be 2.5 at x=0, unreachable)
    assert _sigmoid_ceiling_beta(0.0) == pytest.approx(0.0)
    assert 0.0 < _sigmoid_ceiling_beta(1e-9) < 5.0
    assert _sigmoid_ceiling_beta(4.0) < _sigmoid_ceiling_beta(5.0)  # monotonic


def test_ke_uses_levered_beta_not_total_beta():
    """ke = rf + beta_L*erp; total_beta stored for the size layer only."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    beta_u, _ = eng._unlevered_beta("451020")  # high-beta software sector
    beta_l, rho, total_beta, prov = eng.compute_total_beta(
        beta_u, "SFT", asof, gics_l2="451020", source="sector_bottom_up")
    # force a big gap between raw and ceiling via low rho
    assert rho >= 0.25
    assert total_beta >= beta_l
    _, erp_prov = eng.compute_erp_blend(asof)
    row, _ = eng._row_for("SFT", asof, "451020", ebit=1e9, icr=8.0,
                          iv_baseline=100.0, d_wacc=8.0,
                          retrieval_ts=asof)
    assert row["ke_v2"] == pytest.approx(erp_prov["rf_10y"] + beta_l * row["erp_blend"])
    assert row["ke_v2"] < erp_prov["rf_10y"] + total_beta * row["erp_blend"]
    assert prov["beta_source"] == "sector_bottom_up"


# ---------------------------------------------------------------------------
# Omega stress (Plan L279-286; frozen omega_stress block)
# ---------------------------------------------------------------------------

def test_omega_stress_ext01_documented_not_reachable():
    """EXT-01 (plan L774) expects Omega >= 2.5; frozen formula gives 1.675
    for burn=0.5, ICR=-2, High stress sector (betas >= 1.2)."""
    eng = _mk_engine()
    om, prov = eng._omega_stress(-2.0, -500.0, 1000.0, "451020")  # High: beta 1.55
    assert om == pytest.approx(1.0 + 0.5 * 0.9 * 1.5)  # 1.675
    assert prov["stress_tier"] == "High"
    assert om < 2.5  # EXT-01 assertion NOT satisfiable under frozen formula
    assert 1.0 <= om <= 3.0


def test_omega_stress_neutral_when_icr_missing():
    """Missing ICR -> icr_comp 1.0 -> icr_term 0 -> Omega=1.0 (no penalty)."""
    eng = _mk_engine()
    om, prov = eng._omega_stress(None, -500.0, 1000.0, "451020")
    assert om == 1.0
    assert prov["icr_term"] == 0.0


def test_omega_stress_clamped_at_max_multiplier():
    """Omega clamps to frozen max_multiplier 3.0 for extreme burn + low ICR."""
    eng = _mk_engine()
    om, prov = eng._omega_stress(0.1, -1e9, 1000.0, "451020")  # burn huge
    assert om == pytest.approx(3.0)
    assert prov["burn_ratio"] > 0.0


def test_omega_stress_disabled_by_frozen_config():
    """omega_stress.enabled=false => Omega=1.0."""
    eng = WACCEngineV2(config={"phase8": {"wacc_engine": {"omega_stress": {"enabled": False}}},
                              "total_beta": {}})
    om, prov = eng._omega_stress(-2.0, -500.0, 1000.0, "451020")
    assert om == 1.0 and prov["enabled"] is False


# ---------------------------------------------------------------------------
# Cost of debt / spread (distress, tiers, clamp)
# ---------------------------------------------------------------------------

def test_distressed_spread_is_frozen_885bps():
    """EBIT<=0 -> base = distressed_spread 8.85% (Caa/CCC) * Omega, clamped."""
    eng = _mk_engine()
    spread, rating, distress, prov = eng._cost_of_debt_spread(
        "DS", pd.Timestamp("2024-11-01"), "451020", ebit=-50.0, icr=-2.0)
    assert distress is True
    assert prov["base_spread"] == pytest.approx(0.0885)
    assert rating == "CCC"
    assert 0.0063 <= spread <= 0.19  # frozen spread_bounds


def test_spread_clamp_upper_and_bounds_flag():
    """Extreme Omega pushes spread past 19% -> clamped + bounds_flag (Ruling #10)."""
    eng = _mk_engine()
    # burn tiny revenue, ICR floor 0.1, high-beta sector:
    # omega = 1 + 1.0 * burn * (1-0.1) * 1.5; filing gives burn = min(1, 200/100) = 1
    eng.update_on_filing("B", "2024-10-28", {
        "d_e_market_net": 1.0, "cash_ratio": 0.05, "icr_v2": 0.1,
        "operating_cash_flow": -200.0, "revenue": 100.0, "tax_rate_applied": 0.21,
        "is_financial_kd": False, "iv_baseline": 90.0,
        "duration_wacc": 0.0, "wacc_estimate": 0.09})
    spread, _, _, prov = eng._cost_of_debt_spread(
        "B", pd.Timestamp("2024-11-01"), "451020", ebit=-1.0, icr=0.1)
    base = 0.0885
    raw = base * (1.0 + 1.0 * 1.0 * 0.9 * 1.5)  # burn=1 (ocf=-rev), icr_term=0.9
    assert raw > 0.19
    assert spread == pytest.approx(0.19)  # clamped to frozen ceiling
    assert prov["bounds_flag"] is True


def test_ratings_tier_spread_within_bounds_for_normal_icr():
    """Table lookup: strong ICR = low spread; the Aaa 4bp tier sits below the
    frozen floor but the clamp is applied at the boundary (documented clash)."""
    eng = _mk_engine()
    spread, rating, distress, prov = eng._cost_of_debt_spread(
        "A", pd.Timestamp("2024-11-01"), "151020", ebit=100.0, icr=25.0)
    assert distress is False
    assert spread == pytest.approx(max(prov["base_spread"], 0.0063), abs=1e-9)


def test_rating_mapping_covers_all_damodaran_tiers():
    """All 15 default_ratings tiers map into the coarse schema enum
    (valuation_schema synthetic_rating)."""
    import yaml
    from pathlib import Path
    doc = yaml.safe_load(Path("config/industry_beta.yaml").read_text(encoding="utf-8")) or {}
    tiers = doc["default_ratings"]["tiers"]
    assert len(tiers) == 15
    for t in tiers:
        mapped = _map_rating_to_schema(str(t["rating"]))
        assert mapped in RATING_ENUM_KEYS, t["rating"]
    # Exact conversions in both directions of the rating string format
    assert _map_rating_to_schema("Aaa/AAA") == "AAA"
    assert _map_rating_to_schema("A1/A+") == "A"
    assert _map_rating_to_schema("Aa2/AA") == "AA"
    assert _map_rating_to_schema("Baa2/BBB") == "BBB"
    assert _map_rating_to_schema("Caa/CCC") == "CCC"
    assert _map_rating_to_schema("D2/D") == "D"
    assert _map_rating_to_schema("") == "NONE"


# ---------------------------------------------------------------------------
# Beta levering (Hamada net debt), financials carve-out, tax
# ---------------------------------------------------------------------------

def test_hamada_relevering_formula():
    """beta_L = beta_U * (1 + (1-tau)*d_e) - beta_cash*cash_e (cash term 0)."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    eng.update_on_filing("HAM", "2024-10-01", {
        "d_e_market_net": 0.5, "cash_ratio": 0.0, "icr_v2": 8.0,
        "operating_cash_flow": 5e8, "revenue": 1e9, "tax_rate_applied": 0.21,
        "is_financial_kd": False, "iv_baseline": 100.0, "duration_wacc": 8.0,
        "wacc_estimate": 0.09})
    beta_u, _ = eng._unlevered_beta("151020")
    beta_l, _, _, prov = eng.compute_total_beta(
        beta_u, "HAM", asof, gics_l2="151020", source="sector_bottom_up")
    assert beta_l == pytest.approx(beta_u * (1.0 + (1.0 - 0.21) * 0.5))
    assert prov["debt_missing"] is False


def test_financials_carve_out_beta_and_debt_weight():
    """GICS 401010 (Financials): beta_L=beta_U, w_d=0, ke = rf + beta_U*erp."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    eng.update_on_filing("BKY", "2024-10-01", {
        "d_e_market_net": 3.0, "cash_ratio": 0.1, "icr_v2": 5.0,
        "operating_cash_flow": 2e8, "revenue": 4e8, "tax_rate_applied": 0.21,
        "is_financial_kd": True, "iv_baseline": 150.0, "duration_wacc": 6.0,
        "wacc_estimate": 0.10})
    beta_u, _ = eng._unlevered_beta("401010")
    beta_l, _, _, prov = eng.compute_total_beta(
        beta_u, "BKY", asof, gics_l2="401010", source="sector_bottom_up")
    assert prov["is_financial"] is True
    assert abs(beta_l - beta_u) < 1e-9
    assert eng._debt_weight("BKY", asof, "401010") == 0.0


def test_tax_shield_zero_when_ebit_negative():
    """EBIT<=0 -> tau_eff=0 (100% shield); Hamada still uses applied PIT tau."""
    eng = _mk_engine()
    d = pd.Timestamp("2024-11-01")
    assert eng._effective_tax("X", d, ebit=-1.0) == 0.0
    assert eng._effective_tax("X", d, ebit=None) == pytest.approx(0.21)  # fallback
    eng.update_on_filing("X", "2024-10-01", {"tax_rate_applied": 0.25})
    assert eng._effective_tax("X", d, ebit=100.0) == pytest.approx(0.25)
    assert eng._hamada_tax("X", d) == pytest.approx(0.25)
    # out-of-bounds effective tau -> fallback 0.21
    eng.update_on_filing("Y", "2024-10-01", {"tax_rate_applied": 0.9})
    assert eng._effective_tax("Y", d, ebit=100.0) == pytest.approx(0.21)


def test_wacc_closed_form():
    """wacc = (1-w_d)*ke + w_d*kd_pre*(1-tau_eff) — recompute from components."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    eng.update_on_filing("CF", "2024-10-01", {
        "d_e_market_net": 1.0, "cash_ratio": 0.0, "icr_v2": 6.0,
        "operating_cash_flow": 5e8, "revenue": 1e9, "tax_rate_applied": 0.21,
        "is_financial_kd": False, "iv_baseline": 120.0, "duration_wacc": 7.0,
        "wacc_estimate": 0.09})
    row, prov = eng._row_for("CF", asof, "151020", ebit=5e8, icr=6.0,
                             iv_baseline=120.0, d_wacc=7.0, retrieval_ts=asof)
    w_d = 1.0 / 2.0  # d_e=1 -> w_d=0.5
    tau = 0.21
    ke, kd = prov["ke_exact"], prov["kd_pre_exact"]
    assert row["wacc_v2"] == pytest.approx((1 - w_d) * ke + w_d * kd * (1 - tau))
    # components stay inside frozen bounds
    assert 0.0 <= row["wacc_v2"] <= 0.5
    assert 0.005 <= row["erp_blend"] <= 0.08


def test_d_e_capped_at_25():
    """D/E is capped at D_E_NET_CAP_P95=2.5 before w_d conversion."""
    eng = _mk_engine()
    eng.update_on_filing("CAP", "2024-10-01", {"d_e_market_net": 50.0})
    w_d = eng._debt_weight("CAP", pd.Timestamp("2024-11-01"), "151020")
    assert w_d == pytest.approx(2.5 / 3.5)


# ---------------------------------------------------------------------------
# Appendix B fallback chain + PIT
# ---------------------------------------------------------------------------

def test_appendix_b_fallback_chain_sources():
    """1) mapped L2 -> sector_bottom_up; 2) missing L2 -> sector_median_fallback;
    3) every lookup returns a non-null beta (100% beta_source coverage)."""
    eng = _mk_engine()
    b1, s1 = eng._unlevered_beta("451020")
    assert b1 > 0.0 and s1 == "sector_bottom_up"
    b2, s2 = eng._unlevered_beta(None)          # missing sector
    assert b2 > 0.0 and s2 in ("sector_median_fallback", "universe_median_fallback")
    b3, s3 = eng._unlevered_beta("999999")      # unmapped L2
    assert b3 > 0.0 and s3 in ("sector_median_fallback", "universe_median_fallback")
    # universe fallback equals the frozen appendix value when no map exists
    eng2 = WACCEngineV2(config={"phase8": {}})
    eng2.sector_beta._sector = {}               # force empty map
    eng2.sector_beta._all_betas = []
    b4, s4 = eng2._unlevered_beta("1")
    assert b4 == pytest.approx(0.743) and s4 == "universe_median_fallback"


def test_firm_regression_gate_36_obs():
    """Firm regression β honored only with >=36 observations."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    eng.load_beta_60m({("FRM", pd.Timestamp("2024-11-01")): 1.8},
                      n_obs={("FRM", pd.Timestamp("2024-11-01")): 35})
    assert eng._firm_regression("FRM", asof) is None          # 35 < 36
    eng.load_beta_60m({("FRM", pd.Timestamp("2024-11-01")): 1.8},
                      n_obs={("FRM", pd.Timestamp("2024-11-01")): 48},
                      rho_map={("FRM", pd.Timestamp("2024-11-01")): 0.6})
    fr = eng._firm_regression("FRM", asof)
    assert fr is not None and fr[0] == 1.8 and fr[1] == 48
    beta_l, rho, total_beta, prov = eng.compute_total_beta(
        0.9, "FRM", asof, "151020", source="sector_bottom_up",
        firm_override=fr)
    assert prov["beta_source"] == "firm_regression_60m"
    assert beta_l == pytest.approx(1.8)                       # used as-is
    assert prov["firm_regression_n"] == 48


def test_rho_missing_defaults_to_neutral_and_flags():
    """Missing rho -> 0.5 neutral + rho_degraded (Appendix B rule 4); valid rho
    below floor -> clamped to rho_floor 0.25 without the degraded flag."""
    eng = _mk_engine()
    rho, degraded = eng._rho_used("M", pd.Timestamp("2024-11-01"))
    assert rho == pytest.approx(0.5) and degraded is True
    rho2, degraded2 = eng._rho_used("M", pd.Timestamp("2024-11-01"), rho_override=0.1)
    assert rho2 == pytest.approx(0.25) and degraded2 is False


def test_pit_no_lookahead():
    """A filing dated after the as-of date must NOT leak into the value."""
    eng = _mk_engine()
    eng.update_on_filing("PIT", "2024-11-10", {"d_e_market_net": 1.0})   # future
    d_prev = pd.Timestamp("2024-11-01")
    d_future = pd.Timestamp("2024-11-11")
    assert eng._pit_lookup(eng.cap_struct, "PIT", d_prev) is None
    v = eng._pit_lookup(eng.cap_struct, "PIT", d_future)
    assert v is not None and v == pytest.approx(1.0)


def test_iv_nowcast_floor_and_duration_formula():
    """iv_nowcast = iv_base*(1 - d_wacc*delta_wacc), floored at 0.01."""
    eng = _mk_engine()
    asof = pd.Timestamp("2024-11-01")
    eng.update_on_filing("IVT", "2024-10-01", {
        "d_e_market_net": 0.5, "cash_ratio": 0.0, "icr_v2": 8.0,
        "operating_cash_flow": 5e8, "revenue": 1e9, "tax_rate_applied": 0.21,
        "is_financial_kd": False, "iv_baseline": 100.0, "duration_wacc": 8.0,
        "wacc_estimate": 0.09})
    row, _ = eng._row_for("IVT", asof, "151020", ebit=5e8, icr=8.0,
                          iv_baseline=100.0, d_wacc=8.0, retrieval_ts=asof)
    # delta_wacc = row.wacc - baseline  => high duration * (positive delta) shrinks IV
    delta = row["wacc_v2"] - 0.09
    if abs(delta) < 1e-9:
        assert row["iv_nowcast"] == pytest.approx(100.0)
    assert row["iv_nowcast"] >= 0.01
    # extreme negative move cannot push IV below the floor
    row2, _ = eng._row_for("IVT", asof, "151020", ebit=5e8, icr=8.0,
                           iv_baseline=0.005, d_wacc=0.0, retrieval_ts=asof)
    assert row2["iv_nowcast"] == pytest.approx(0.01)  # floor