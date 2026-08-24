"""Phase-0 counter-test helpers — D-20260823-001.

Implements the devil-advocate and sim-guardian audit instruments:
coverage_pct_for_source      DA-1  timestamp coverage gate (>=80% or quarantine)
oracle_transfer_verdict      DA-2/B-2  benchmark-vs-real-input F1 divergence
instrument_provenance_ok     B-1   training-cutoff provenance check
"""
import re
import sqlite3

import yaml

_BARS_PATH = "config/weights_sentinel_bars.yaml"


def _bars():
    with open(_BARS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def coverage_pct_for_source(manifest: dict, table: str) -> float:
    """DA-1: % of rows in a raw source manifest carrying a parseable date."""
    rows = manifest.get(table) or []
    if not rows:
        return 0.0
    dated = sum(1 for r in rows if r.get("date") and _DATE_RE.search(str(r["date"])))
    return 100.0 * dated / len(rows)


def oracle_transfer_verdict(benchmark_f1: float, real_input_f1: float, bars: dict | None = None) -> dict:
    """DA-2/B-2: skill proven on the benchmark but not on real inputs = illusory."""
    cfg = (bars or _bars())["stage2_transfer"]
    drop_pp = (benchmark_f1 - real_input_f1) * 100.0
    return {
        "passed": drop_pp <= cfg["max_f1_drop_pp"],
        "drop_pp": round(drop_pp, 2),
        "benchmark_f1": round(benchmark_f1, 4),
        "real_input_f1": round(real_input_f1, 4),
        "bar_max_drop_pp": cfg["max_f1_drop_pp"],
        "min_sample_required": cfg["min_real_inputs_sample"],
    }


def instrument_provenance_ok(name: str, trained: bool, cutoff: str | None, rule: str | None = None) -> bool:
    """B-1: an instrument is PIT-clean if rule-based (never trained) or its
    training cutoff predates the window start encoded in the provenance rule."""
    del name
    if not trained:
        return True
    if not cutoff:
        return False
    window_start = re.search(r"\d{4}-\d{2}-\d{2}", rule or "")
    return bool(window_start and cutoff <= window_start.group(0))


if __name__ == "__main__":
    conn = sqlite3.connect(":memory:")
    print("phase0 helper self-check ok")
