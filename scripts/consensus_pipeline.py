"""Anti-bias alt-data consensus gate CLI (D-20260816-001, P1, research-only).

Sorts per-company alt-data evidence into the frozen composite and writes
auditable rows to the additive consensus tables. Research-only: nothing here
touches the qualitative gate thresholds or any frozen core.

Usage:
    python scripts/consensus_pipeline.py --run          # run live pass (DISCOVERY_LIVE=1)
    python scripts/consensus_pipeline.py --collect      # scrape live metrics per site (research, no gate)
    python scripts/consensus_pipeline.py --seed         # demo run with injected fixture data
    python scripts/consensus_pipeline.py --report       # show last run's sorted rows
    python scripts/consensus_pipeline.py --config-check # validate weights_consensus.yaml

Tickers are passed as TICKER:SECTOR pairs on the command line (or a default
deterministic sample when none given).
"""

import argparse
import asyncio
import sys
import os

from dotenv import load_dotenv

load_dotenv()
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "Qualitative"))

DEFAULT_TICKERS = [
    ("NVDA", "Semiconductors"), ("AMD", "Semiconductors"),
    ("MSFT", "Software"), ("AAPL", "Consumer"),
]


def _seed_collectors():
    """Deterministic fixture collectors so the demo never touches the network.

    Differentiates per ticker so flags (SET-ASIDE, BRIBE-ATTACK, polarization)
    and the two-extreme normalization actually surface in the demo output.
    """
    from discovery.consensus.collectors import make_review_collector, make_talent_collector, make_quantifiable_collector

    def review_data_for(ticker):
        h = sum(ord(c) for c in ticker) % 1000
        if ticker == "AAPL":
            # Healthy, widely-reviewed company: all sources usable, agreeing.
            return {
                "glassdoor": {"n": 240, "star_level": 4.2, "skewness": 0.4, "iqr": 1.0,
                              "recent_weekly_volume": 12.0, "normal_weekly_volume": 10.0},
                "indeed": {"n": 180, "star_level": 3.9, "skewness": 0.1, "iqr": 1.2},
                "g2": {"n": 130, "star_level": 4.5, "skewness": 0.6, "iqr": 0.8},
                "capterra": {"n": 110, "star_level": 4.1, "skewness": 0.2, "iqr": 1.1},
                "trustpilot": {"n": 95, "star_level": 3.4, "skewness": -0.5, "iqr": 1.5},
            }
        if ticker == "AMD":
            # Bribe-burst victim: a cluster of bought 5-star reviews.
            return {
                "glassdoor": {"n": 150, "star_level": 3.8, "skewness": 0.5, "iqr": 1.1,
                              "records": _burst_records(5)},
                "g2": {"n": 90, "star_level": 4.0, "skewness": 0.3, "iqr": 0.9},
                "trustpilot": {"n": 120, "star_level": 3.2, "skewness": 0.2, "iqr": 1.6},
            }
        if ticker == "MSFT":
            # Company-punishing attack: a coordinated 1-star barrage spike.
            return {
                "glassdoor": {"n": 200, "star_level": 4.0, "skewness": 0.3, "iqr": 1.0,
                              "recent_weekly_volume": 45.0, "normal_weekly_volume": 10.0},
                "indeed": {"n": 160, "star_level": 3.7, "skewness": 0.2, "iqr": 1.3},
                "trustpilot": {"n": 140, "star_level": 3.0, "skewness": -0.3, "iqr": 1.7},
            }
        # NVDA: SET-ASIDE candidate (<50 reviews across all platforms).
        return {
            "glassdoor": {"n": 12, "star_level": 4.5, "skewness": 0.2, "iqr": 0.8},
            "trustpilot": {"n": 20, "star_level": 3.6, "skewness": -0.1, "iqr": 1.4},
        }

    def make_rc(site):
        def _collect(company):
            data = review_data_for(company).get(site)
            if data is None:
                return None
            return dict(data)
        return _collect

    collectors = {s: make_rc(s) for s in ["glassdoor", "indeed", "g2", "capterra", "trustpilot"]}

    def talent(company):
        # MSFT sees a senior-talent influx (positive signal).
        if company == "MSFT":
            return {"senior_mentions": 10, "hiring_velocity": 140.0}
        return {"senior_mentions": 2, "hiring_velocity": 90.0}

    def quantifiable(company):
        h = sum(ord(c) for c in company)
        return {
            "transaction_volume": 0.5 + (h % 50) / 100.0,
            "sec_attrition_velocity": 0.3 + (h % 30) / 100.0,
        }

    return collectors, talent, quantifiable


def _burst_records(star, count=6, base_ts=1_700_000_000):
    """Deterministic same-star burst from new profiles (bribe-attack pattern)."""
    from discovery.consensus.engine import ReviewRecord
    return [ReviewRecord(star=star, ts=base_ts + i * 600, profile_bucket="new")
            for i in range(count)]


def main():
    parser = argparse.ArgumentParser(description="Consensus gate pipeline (research-only)")
    parser.add_argument("--run", action="store_true", help="run a live pass")
    parser.add_argument("--collect", action="store_true", help="scrape live review metrics per site (research)")
    parser.add_argument("--seed", action="store_true", help="demo pass with fixtures")
    parser.add_argument("--report", action="store_true", help="show last run rows")
    parser.add_argument("--config-check", action="store_true", help="validate config only")
    parser.add_argument("tickers", nargs="*", help="TICKER:SECTOR pairs")
    args = parser.parse_args()

    if args.config_check:
        from discovery.consensus.config import load_consensus_config
        cfg = load_consensus_config()
        print(f"config OK: consensus.enabled={cfg['consensus']['enabled']}, "
              f"blocks={cfg['weights']['blocks']}")
        return

    tickers = []
    for t in args.tickers or []:
        if ":" in t:
            tick, sec = t.split(":", 1)
            tickers.append((tick.strip().upper(), sec.strip()))
    if not tickers:
        tickers = DEFAULT_TICKERS

    if args.seed:
        collectors, talent, quant = _seed_collectors()
        from discovery.consensus.pipeline import run_consensus_pass
        rows = asyncio.run(run_consensus_pass(
            tickers,
            review_collectors=collectors,
            talent_collector=talent,
            quantifiable_collector=quant,
        ))
        from discovery.consensus.store import persist_consensus_run
        run_ts = persist_consensus_run(rows)
        print(f"seed run persisted (run_ts={run_ts})")
        for r in rows:
            print(f"  {r.ticker:6s} {r.sector:14s} score={r.composite_score:.3f} "
                  f"flags={r.flags} reviews={r.total_reviews} conv={r.converged}")
        return

    if args.collect:
        from discovery.consensus.config import load_consensus_config
        from discovery.consensus.collectors import make_review_collector, make_talent_collector, make_quantifiable_collector

        cfg = load_consensus_config()
        sites = ["glassdoor", "indeed", "g2", "capterra", "comparably"]
        rc = {s: make_review_collector(s) for s in sites}
        print(f"live scrape (research): {len(tickers)} tickers x {len(sites)} sites "
              f"(DISCOVERY_LIVE={os.environ.get('DISCOVERY_LIVE')})")
        for ticker, sector in tickers:
            print(f"-- {ticker} ({sector}) --")
            for s in sites:
                try:
                    data = asyncio.run(rc[s](ticker))
                    if data is None:
                        print(f"   {s:12s} no evidence")
                        continue
                    n = int(data.get("n", 0))
                    star = data.get("star_level")
                    print(f"   {s:12s} n={n:<6} star={star if star is not None else 'N/A'}")
                except Exception as exc:
                    print(f"   {s:12s} ERROR {exc}")
        return

    if args.run:
        from discovery.consensus.config import load_consensus_config
        from discovery.consensus.pipeline import run_consensus_pass
        cfg = load_consensus_config()
        if not cfg["consensus"]["enabled"]:
            print("consensus.enabled=false — research-only kill-switch. "
                  "Refusing to run live pass. (Set after a separate APPROVE ruling.)")
            sys.exit(1)
        rows = asyncio.run(run_consensus_pass(tickers))
        from discovery.consensus.store import persist_consensus_run
        run_ts = persist_consensus_run(rows)
        print(f"live run persisted (run_ts={run_ts})")
        for r in rows:
            print(f"  {r.ticker:6s} {r.sector:14s} score={r.composite_score:.3f} "
                  f"flags={r.flags}")
        return

    if args.report:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.execute(
            "SELECT run_ts, MAX(run_ts) FROM consensus_company_rows GROUP BY run_ts ORDER BY run_ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            print("no consensus runs yet")
            return
        run_ts = row["run_ts"]
        print(f"last run: {run_ts}")
        for r in conn.execute(
            "SELECT ticker, sector, composite_score, flags, converged, normalized "
            "FROM consensus_company_rows WHERE run_ts=? ORDER BY composite_score DESC",
            (run_ts,),
        ):
            print(f"  {r['ticker']:6s} {r['sector']:14s} score={r['composite_score']:.3f} "
                  f"flags={r['flags']} conv={r['converged']} norm={r['normalized']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()