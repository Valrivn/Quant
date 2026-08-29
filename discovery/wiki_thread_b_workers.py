"""Parallel worker lane for the Thread-B category discovery (D-20260828-001).

Thread A (frontier expansion: CPU-bound) and Thread B's industry-member fetch
(network-bound Wikidata I/O) are independent strips of work run CONCURRENTLY on
a bounded ThreadPoolExecutor. After both complete, Thread B's deterministic
candidate draw + merge run (they need Thread A's reached-company set).

A second helper parallelizes the discovery-screen over Thread-B NOVEL
candidates (per-candidate qual+quant gate), settling fill discipline: every
novel candidate still routes through the existing screen before surfacing.

All callables are injected so the concurrency logic is unit-testable without
network/DB side effects and without changing the deterministic frontier result.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

from discovery.wiki_frontier import (
    build_thread_b_candidates,
    merge_thread_b,
    WikiFrontierResult,
    WikiNode,
)


def run_threads_parallel(
    frontier_callable: Callable[[], WikiFrontierResult],
    seed_industries_callable: Callable[[List[str]], Dict],
    companies: Dict[str, str],
    ticker_industry: Optional[Dict[str, str]],
    industry_members_raw: Optional[Dict],
    industry_beta_cfg: dict,
    thread_b_cfg: dict,
    seed_qids: List[str],
    company_cap: int,
    peer_members_callable: Optional[Callable[[List[str]], Dict]] = None,
    max_workers: int = 2,
) -> Dict:
    """Run Thread A and the Thread-B seed-industry fetch concurrently, then merge.

    ``frontier_callable`` must return a ``WikiFrontierResult`` (Thread A).
    ``seed_industries_callable(seed_qids)`` must return the bounded
    ``fetch_company_industries(seed_qids)`` payload (network, runs in parallel
    with Thread A). ``peer_members_callable(peer_labels)`` must return the
    bounded ``fetch_industry_members(industry_labels=peer_labels)`` payload, or
    None to skip the peer-member draw entirely (Thread B yields no novel names).
    bounded ``fetch_industry_members(industry_labels=peer_labels)`` payload.

    Flow:
      1. Thread A (frontier) || seed-industry fetch run CONCURRENTLY.
      2. Resolve seed industries -> Damodaran industries (local, no network).
      3. Derive peer industries in the same beta-band with a different sub-area
         (local; beta banding, never network).
      4. Bounded peer-member fetch (network) -> deterministic draw + merge.

    Returns dict with 'result_a', 'thread_b_candidates', 'b_intersections',
    'thread_a_nodes' and 'merged_qids'.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        a_future = ex.submit(frontier_callable)
        b_future = ex.submit(seed_industries_callable, list(seed_qids))

        result_a = a_future.result()
        seed_payload = b_future.result()

    # Resolve reached (Thread A) company QIDs.
    thread_a_nodes = [
        n for n in result_a.nodes if n.kind == "company" and companies.get(n.qid)
    ]
    reached = {n.qid for n in thread_a_nodes}

    # Seed/company -> industry map (bounded, from seed query + reached overlap).
    company_industry = dict(seed_payload.get("company_industry", {}))
    payload_ticker_industry: Dict[str, str] = {}
    for qid, ind in company_industry.items():  # qid-keyed -> ticker-keyed
        t = companies.get(qid)
        if t and ind:
            payload_ticker_industry.setdefault(t, ind)
    resolved_ticker_industry = {
        **payload_ticker_industry,
        **(ticker_industry or {}),
    }

    # Determine peer industries: same unlevered-beta band, different sub-area,
    # derived locally from the config (no network).
    seed_industries = {
        ind for ind in resolved_ticker_industry.values()
        if _beta_known(ind, industry_beta_cfg)
    }
    peer_keys = _peer_industries(seed_industries, industry_beta_cfg, thread_b_cfg)
    peer_labels = _wikidata_labels_for_peers(peer_keys, industry_beta_cfg)

    # Bounded peer-member fetch (network). Failure degrades to empty, never raising.
    members = {}
    if peer_labels and peer_members_callable is not None:
        try:
            members = peer_members_callable(sorted(peer_labels)).get(
                "industry_members", {}
            )
        except Exception:  # noqa: BLE001 - peer fetch is best-effort research
            members = {}

    cands = build_thread_b_candidates(
        reached_companies=reached | set(company_industry.keys()),
        companies=companies,
        ticker_industry=resolved_ticker_industry or None,
        industry_members=members,
        cfg=industry_beta_cfg,
        beta_band=float(thread_b_cfg.get("beta_band", 0.15)),
        prefer_different_sub_area=bool(
            thread_b_cfg.get("prefer_different_sub_area", True)
        ),
        seeds_include=set(seed_qids),
    )

    merged, intersections = merge_thread_b(
        thread_a_nodes=thread_a_nodes,
        thread_b_candidates=cands,
        company_cap=company_cap,
        randomized=thread_b_cfg.get("randomized"),
    )

    return {
        "result_a": result_a,
        "thread_b_candidates": cands,
        "b_intersections": intersections,
        "thread_a_nodes": thread_a_nodes,
        "merged_qids": [n.qid for n in merged],
    }


def _beta_known(industry: str, cfg: Optional[dict]) -> bool:
    """True if the config holds an unlevered-beta fingerprint for the industry."""
    if cfg is None:
        return False
    from discovery.wiki_frontier import _normalize_industry
    normalized = _normalize_industry(industry, cfg)
    entry = cfg.get("industries", {}).get(normalized)
    if not isinstance(entry, dict):
        return False
    beta = entry.get("unlevered_beta")
    return isinstance(beta, (int, float)) and beta > 0


def _peer_industries(seed_industries: set, cfg: Optional[dict],
                     thread_b_cfg: dict) -> set:
    """Same beta-band, different sub-area industries reachable from any seed industry.
    Local computation only; never network."""
    from discovery.wiki_frontier import _beta_for

    if cfg is None or not seed_industries:
        return set()
    band = float(thread_b_cfg.get("beta_band", 0.15))
    prefer_diff = bool(thread_b_cfg.get("prefer_different_sub_area", True))
    peers = set()
    for seed_ind in seed_industries:
        anchor_beta = _beta_for(seed_ind, cfg)
        anchor_sub = _canonical_sub_area_local(seed_ind, cfg)
        if anchor_beta is None:
            continue
        for cand, entry in cfg.get("industries", {}).items():
            if cand == seed_ind:
                continue
            b = entry.get("unlevered_beta")
            if not isinstance(b, (int, float)) or abs(b - anchor_beta) > band:
                continue
            if prefer_diff:
                cand_sub = entry.get("sub_area")
                if anchor_sub and cand_sub and cand_sub == anchor_sub:
                    continue
            peers.add(cand)
    return peers


def _canonical_sub_area_local(industry: str, cfg: Optional[dict]) -> Optional[str]:
    if cfg is None:
        return None
    from discovery.wiki_frontier import _normalize_industry
    normalized = _normalize_industry(industry, cfg)
    entry = cfg.get("industries", {}).get(normalized)
    if isinstance(entry, dict):
        return entry.get("sub_area")
    aliases = cfg.get("sub_area_aliases", {})
    return aliases.get(normalized)


def _wikidata_labels_for_peers(peer_keys: set, cfg: Optional[dict]) -> set:
    """Reverse-map Damodaran industry keys back to Wikidata industry labels.

    Uses ``industry_aliases`` to resolve which raw strings from P452 map onto
    the target peer keys. Fallbacks to the keys themselves and lowercased keys.
    """
    if cfg is None:
        return set()
    aliases = cfg.get("industry_aliases", {})
    labels = set()
    for wk, dk in aliases.items():
        if dk in peer_keys:
            labels.add(wk)
    for pk in peer_keys:
        labels.add(pk)
        labels.add(pk.lower())
    return labels


def screen_novel_parallel(
    thread_b_candidates: Dict[str, dict],
    thread_a_nodes: List[WikiNode],
    screen_fn: Callable[[str], bool],
    max_workers: int = 1,
) -> Dict[str, Dict]:
    """Run the existing discovery screen over Thread-B NOVEL candidates in parallel.

    ``screen_fn(ticker) -> bool`` is the existing qual+quant gate (e.g. via
    discovery/gate_data.py / screen-worker). Only candidates NOT already in
    Thread A are screened (a steal needs no attention). Returns
    ``{ticker: {'qid','passed','grade','industry','sub_area'}}`` sorted by
    (passed desc, grade desc) for deterministic downstream consumption.
    """
    a_qids = {n.qid for n in thread_a_nodes}
    novel = [
        (q, v) for q, v in thread_b_candidates.items() if q not in a_qids
    ]

    results: Dict[str, Dict] = {}
    if not novel:
        return results

    def _work(item: Tuple[str, dict]):
        q, v = item
        ticker = v["ticker"]
        try:
            passed = bool(screen_fn(ticker))
        except Exception:
            passed = False
        return ticker, {
            "qid": q,
            "passed": passed,
            "grade": float(v["grade"]),
            "industry": v.get("industry", ""),
            "sub_area": v.get("sub_area", ""),
        }

    with ThreadPoolExecutor(max_workers=max(max_workers, 1)) as ex:
        futures = [ex.submit(_work, item) for item in novel]
        for fut in as_completed(futures):
            ticker, info = fut.result()
            results[ticker] = info

    # Deterministic ordering for consumption: passed first, then grade desc.
    ordered = sorted(
        results.items(),
        key=lambda kv: (not kv[1]["passed"], -kv[1]["grade"], kv[0]),
    )
    return {t: info for t, info in ordered}
