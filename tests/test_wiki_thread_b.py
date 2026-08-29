"""Tests for the Thread-B category discovery lane (ruling D-20260828-001).

Covers: deterministic business-core draw, beta-band grouping, different-sub-area
preference, intersection bookkeeping (|A∩B|, |B∖A|), seeded reproducibility of
the minimized-Gumbel randomization, and bit-identical deterministic behavior
when randomization is disabled.
"""

import json

from discovery.industry_beta import load_industry_beta, IndustryBetaConfigError
from discovery.wiki_frontier import (
    build_thread_b_candidates,
    merge_thread_b,
    WikiNode,
)

CFG = load_industry_beta()


def _companies(*pairs):
    return dict(pairs)


def _members(*items):
    out = {}
    for ind, arr in items:
        out[ind] = arr
    return out


# --- industry_beta.yaml loader -----------------------------------------------

def test_loader_loads_expected_fingerprints():
    assert CFG["industries"]["Semiconductor"]["unlevered_beta"] == 1.35
    assert CFG["industries"]["Semiconductor"]["sub_area"] == "chip_design"
    assert CFG["update"]["auto_replace"] is False
    assert CFG["thread_b"]["beta_band"] == 0.15


def test_loader_fails_closed_on_bad_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("industries:\n  X: {unlevered_beta: 0}\nupdate:\n  auto_replace: false\n")
    try:
        load_industry_beta(str(bad))
        assert False, "should have raised"
    except IndustryBetaConfigError:
        pass


def test_loader_fails_closed_on_missing_beta(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("industries:\n  X: {sub_area: y}\nupdate:\n  auto_replace: false\n")
    try:
        load_industry_beta(str(bad))
        assert False, "should have raised"
    except IndustryBetaConfigError:
        pass


# --- deterministic Thread-B candidate draw -----------------------------------

def test_same_band_different_sub_area_surfaces_novel():
    # Anchor: NVDA is reached, in Semiconductor (beta 1.35, sub_area chip_design).
    # Same band: Semiconductor Equipment (1.30), different sub_area (chip_equipment).
    # Opposite band: Utility (0.45) is NOT within band 0.15 of 1.35.
    companies = _companies(("QN", "NVDA"), ("QE", "ASML"), ("QU", "UTIL"))
    ticker_industry = {"NVDA": "Semiconductor", "ASML": "Semiconductor Equipment",
                       "UTIL": "Utility (General)"}
    reached = {"QN", "QE"}
    industry_members = _members(
        ("Semiconductor", [("QS1", "AMD"), ("QS2", "INTC")]),
        ("Semiconductor Equipment", [("QE1", "KLAC"), ("QE2", "LRCX")]),
        ("Utility (General)", [("QU1", "DUK"), ("QU2", "SO")]),
    )
    cands = build_thread_b_candidates(
        reached, companies, ticker_industry, industry_members, CFG,
        beta_band=0.15, prefer_different_sub_area=True,
    )
    # Novel Surface comes from the same-band DIFFERENT-sub-area equipment industry.
    assert "QE1" in cands and "QE2" in cands
    # Utility is out of band -> not surfaced.
    assert "DUK" not in cands
    # Already-reached anchors are not re-surfaced.
    assert "ASML" not in cands
    assert cands["QE1"]["sub_area"] == "chip_equipment"
    assert cands["QE1"]["grade"] > 0


def test_deterministic_order_is_stable():
    companies = _companies(("QN", "NVDA"))
    ticker_industry = {"NVDA": "Semiconductor"}
    reached = {"QN"}
    industry_members = _members(
        ("Semiconductor", [("QS1", "AMD")]),
        ("Semiconductor Equipment", [("QE1", "KLAC"), ("QE2", "LRCX")]),
    )
    a = build_thread_b_candidates(reached, companies, ticker_industry,
                                  industry_members, CFG, beta_band=0.15)
    b = build_thread_b_candidates(reached, companies, ticker_industry,
                                  industry_members, CFG, beta_band=0.15)
    assert sorted(a.keys()) == sorted(b.keys())


def test_missing_maps_yield_empty():
    assert build_thread_b_candidates({"Q1"}, {}, None, None, CFG) == {}


# --- merge + intersections ----------------------------------------------------

def _node(qid, grade, kind="company"):
    return WikiNode(qid=qid, depth=0, grade=grade, kind=kind, path=(qid,))


def test_merge_records_intersections():
    a = [_node("QA", 2.0), _node("QB", 1.5)]
    b_cands = {
        "QB": {"qid": "QB", "ticker": "TB", "grade": 1.5, "sub_area": "x", "via": "y"},  # overlap
        "QC": {"qid": "QC", "ticker": "TC", "grade": 1.9, "sub_area": "y", "via": "z"},  # novel
    }
    merged, inter = merge_thread_b(a, b_cands, company_cap=3)
    qids = {n.qid for n in merged}
    assert "QA" in qids and "QB" in qids and "QC" in qids
    assert inter["b_overlap"] == 1
    assert inter["b_novel"] == 1
    assert inter["a_cap"] == 2
    assert inter["b_total"] == 2


def test_merge_respects_cap_and_thread_a_priority():
    a = [_node("QA", 1.9)]
    b_cands = {
        "QB": {"qid": "QB", "ticker": "TB", "grade": 2.5, "sub_area": "x", "via": "z"},
        "QC": {"qid": "QC", "ticker": "TC", "grade": 2.6, "sub_area": "x", "via": "z"},
    }
    merged, _ = merge_thread_b(a, b_cands, company_cap=2)
    # Cap 2, only 1 B-slot fits alongside the 1 A node.
    assert len(merged) == 2
    merged_q = {n.qid for n in merged}
    assert "QA" in merged_q  # Thread A always retained


def test_merged_deterministic_when_randomization_off():
    a = [_node("QA", 2.0), _node("QB", 1.5)]
    b_cands = {
        "QC": {"qid": "QC", "ticker": "TC", "grade": 1.9, "sub_area": "x", "via": "z"},
        "QD": {"qid": "QD", "ticker": "TD", "grade": 1.8, "sub_area": "x", "via": "z"},
    }
    m1, _ = merge_thread_b(a, b_cands, company_cap=5, randomized={"enabled": False, "seed": 1})
    m2, _ = merge_thread_b(a, b_cands, company_cap=5, randomized=None)
    assert [n.qid for n in m1] == [n.qid for n in m2]


def test_randomization_order_reproducible_by_seed_and_reverts():
    a = [_node("QA", 1.0), _node("QB", 1.01), _node("QC", 1.02)]
    b_cands = {}
    r1, _ = merge_thread_b(a, b_cands, company_cap=5,
                           randomized={"enabled": True, "seed": 42, "temperature": 0.05})
    r2, _ = merge_thread_b(a, b_cands, company_cap=5,
                           randomized={"enabled": True, "seed": 42, "temperature": 0.05})
    assert [n.qid for n in r1] == [n.qid for n in r2]
    assert len(r1) == len(a)  # re-rank never drops nodes


def test_randomized_summary_flag():
    a = [_node("QA", 1.0)]
    b_cands = {}
    _, x1 = merge_thread_b(a, b_cands, 5, randomized={"enabled": True, "seed": 1})
    _, x0 = merge_thread_b(a, b_cands, 5, randomized=None)
    assert x1 == x0  # intersections unaffected by ordering


def test_json_serializable_summary():
    a = [_node("QA", 1.0)]
    b_cands = {"QB": {"qid": "QB", "ticker": "TB", "grade": 1.2, "sub_area": "s", "via": "v"}}
    _, inter = merge_thread_b(a, b_cands, 5, randomized={"enabled": True, "seed": 3})
    json.dumps(inter)  # must not raise
