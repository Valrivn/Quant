"""Full US-GAAP (and all-taxonomy) XBRL fact extractor.

Supersedes ``extract_raw_xbrl.py`` (22 hardcoded concepts).  This module walks
every cached ``data/pit_fundamentals/<TICKER>/companyfacts.json`` (the SEC
``companyfacts.zip`` payload, ~1.3k CIKs) and emits **every concept, in every
unit, with no filtering** into a single streaming parquet.

Output
------
``data/pit_fundamentals/xbrl_full/raw_xbrl_facts_full.parquet`` with columns::

    ticker, cik, concept, field, value, unit,
    fiscal_end, filed, accession, form, frame, start

* ``concept``  = raw XBRL tag namespaced by taxonomy, e.g.
  ``us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax``.
* ``field``    = ``"val"`` (schema kept compatible with the legacy raw table).
* ``start``    = duration-fact period start (``None`` for instant facts).
  Added beyond the brief so the dense builder can distinguish quarterly /
  year-to-date / annual durations without lookahead.
* ``cik``      = zero-padded 10-digit CIK string (joins ``monthly_reconstitution``).

PIT: ``filed`` is the SEC acceptance date; consumers must anchor on it.
No filtering, no mapping here — see ``ingestion/sec_concept_mapper.py``.

CLI
---
    python -m ingestion.sec_xbrl_full_extractor \
        --input data/pit_fundamentals \
        --output data/pit_fundamentals/xbrl_full/raw_xbrl_facts_full.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

from ingestion.pit_provenance import write_provenance

log = logging.getLogger("sec_xbrl_full_extractor")

SOURCE_VERSION = "companyfacts_20261010"
SOURCE = "SEC_EDGAR"
ENTITY_KEY = "cik"
PIT_COLUMN = "filed"
ROOT = Path(__file__).resolve().parents[1]

EXTRACT_SCHEMA = pa.schema(
    [
        pa.field("ticker", pa.string()),
        pa.field("cik", pa.string()),
        pa.field("concept", pa.string()),
        pa.field("field", pa.string()),
        pa.field("value", pa.float64()),
        pa.field("unit", pa.string()),
        pa.field("fiscal_end", pa.string()),
        pa.field("filed", pa.string()),
        pa.field("accession", pa.string()),
        pa.field("form", pa.string()),
        pa.field("frame", pa.string()),
        pa.field("start", pa.string()),
    ]
)

# Taxonomies observed in companyfacts payloads.  We keep them all ("no
# filtering"), namespacing each concept so downstream can slice by taxonomy.
KNOWN_TAXONOMIES = ("us-gaap", "dei", "ifrs-full", "srt", "ecd", "ffd", "invest", "rxp")


def _pad_cik(raw: object) -> Optional[str]:
    """Normalise a companyfacts CIK to the 10-digit zero-padded string."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s.isdigit():
        return s
    return s.zfill(10)


def iter_companyfacts_files(
    input_dir: Path,
    tickers: Optional[List[str]] = None,
) -> Iterator[Tuple[str, Path]]:
    """Yield ``(ticker, companyfacts.json path)`` sorted by ticker.

    Only directories whose name matches the requested ``tickers`` (if given)
    are considered, then filtered to those that actually contain a
    ``companyfacts.json``.
    """
    wanted = {t.upper() for t in tickers} if tickers else None
    dirs = sorted(
        (d for d in input_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    for d in dirs:
        if wanted is not None and d.name.upper() not in wanted:
            continue
        cf = d / "companyfacts.json"
        if cf.exists():
            yield d.name, cf


def extract_facts_from_companyfacts(
    companyfacts: dict,
    ticker: str,
) -> List[dict]:
    """Extract every fact row present in one companyfacts payload.

    Iterates all taxonomies, all concepts, all units, all entries.  Entries
    without the minimum PIT keys (``val``, ``end``, ``filed``, ``accn``) are
    skipped because they are unusable downstream.  Duplicate
    ``(concept, unit, fiscal_end, filed, accession, start)`` tuples within the
    payload are collapsed.
    """
    facts = companyfacts.get("facts")
    if not isinstance(facts, dict):
        return []

    cik = _pad_cik(companyfacts.get("cik"))
    rows: List[dict] = []
    seen = set()

    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        for tag, tag_data in concepts.items():
            units = (tag_data or {}).get("units")
            if not isinstance(units, dict):
                continue
            concept = f"{taxonomy}:{tag}"
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    val = entry.get("val")
                    end = entry.get("end")
                    filed = entry.get("filed")
                    accn = entry.get("accn")
                    if val is None or end is None or filed is None or accn is None:
                        continue
                    try:
                        fval = float(val)
                    except (TypeError, ValueError):
                        continue
                    start = entry.get("start")
                    key = (concept, unit, end, filed, accn, start)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "ticker": ticker,
                            "cik": cik,
                            "concept": concept,
                            "field": "val",
                            "value": fval,
                            "unit": unit,
                            "fiscal_end": end,
                            "filed": filed,
                            "accession": accn,
                            "form": entry.get("form"),
                            "frame": entry.get("frame"),
                            "start": start,
                        }
                    )
    return rows


def _rows_to_table(rows: List[dict]) -> pa.Table:
    """Build an Arrow table with the fixed extraction schema (column order)."""
    cols = {name: [] for name in EXTRACT_SCHEMA.names}
    for r in rows:
        for name in EXTRACT_SCHEMA.names:
            cols[name].append(r.get(name))
    return pa.table(cols, schema=EXTRACT_SCHEMA)


def run_extraction(
    input_dir: Path,
    output_path: Path,
    *,
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
    batch_tickers: int = 25,
    compression: str = "zstd",
    compute_hash: bool = True,
) -> Dict[str, object]:
    """Stream all companyfacts into one parquet file.

    Returns a summary dict (row_count, ticker_count, concept_count, ...).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Fresh file each run (idempotent extractor).
    if output_path.exists():
        output_path.unlink()

    writer = pq.ParquetWriter(output_path, EXTRACT_SCHEMA, compression=compression)
    n_rows = 0
    tickers_seen = 0
    concepts_seen = set()
    min_filed: Optional[str] = None
    max_filed: Optional[str] = None
    errors: List[str] = []
    t0 = time.time()
    buf: List[dict] = []
    files = list(iter_companyfacts_files(input_dir, tickers))
    if limit is not None:
        files = files[:limit]
    log.info("Extracting from %d companyfacts files under %s", len(files), input_dir)

    for i, (ticker, cf_path) in enumerate(files, 1):
        try:
            with open(cf_path, "r", encoding="utf-8") as fh:
                cf = json.load(fh)
            rows = extract_facts_from_companyfacts(cf, ticker)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
            log.warning("Failed to parse %s: %s", ticker, exc)
            continue

        if rows:
            tickers_seen += 1
            buf.extend(rows)
            for r in rows:
                concepts_seen.add(r["concept"])
                filed = r["filed"]
                if min_filed is None or filed < min_filed:
                    min_filed = filed
                if max_filed is None or filed > max_filed:
                    max_filed = filed

        if len(buf) >= 250_000 or i % batch_tickers == 0:
            if buf:
                writer.write_table(_rows_to_table(buf))
                n_rows += len(buf)
                buf = []

        if i % 100 == 0 or i == len(files):
            elapsed = time.time() - t0
            log.info(
                "  %d/%d tickers | %s rows | %.0fs (%.1f tick/min)",
                i, len(files), f"{n_rows + len(buf):,}", elapsed,
                (i / elapsed) * 60 if elapsed else 0.0,
            )

    if buf:
        writer.write_table(_rows_to_table(buf))
        n_rows += len(buf)
    writer.close()

    summary = {
        "row_count": n_rows,
        "ticker_count": tickers_seen,
        "concept_count": len(concepts_seen),
        "date_min": min_filed,
        "date_max": max_filed,
        "errors": errors,
        "elapsed_sec": round(time.time() - t0, 1),
        "output": str(output_path),
    }
    log.info(
        "Extraction complete: %s rows | %d tickers | %d concepts | %.1fs",
        f"{n_rows:,}", tickers_seen, len(concepts_seen), summary["elapsed_sec"],
    )
    if errors:
        log.warning("%d ticker(s) failed to parse", len(errors))

    # Provenance sidecar + manifest.
    write_provenance(
        output_path,
        source=SOURCE,
        source_version=SOURCE_VERSION,
        pit_timestamp_column=PIT_COLUMN,
        entity_key=ENTITY_KEY,
        transformations=["xbrl_full_extract", "no_concept_filter", "streaming_write"],
        row_count=n_rows,
        date_range={"min": min_filed, "max": max_filed},
        mapping_confidence="high",
        extra={
            "input_dir": str(input_dir).replace("\\", "/"),
            "ticker_count": tickers_seen,
            "concept_count": len(concepts_seen),
            "compression": compression,
            "failed_tickers": errors[:50],
        },
        compute_sha256=compute_hash,
    )
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/pit_fundamentals", help="companyfacts cache root")
    parser.add_argument(
        "--output",
        default="data/pit_fundamentals/xbrl_full/raw_xbrl_facts_full.parquet",
        help="output parquet path",
    )
    parser.add_argument("--tickers", nargs="*", default=None, help="optional ticker subset")
    parser.add_argument("--limit", type=int, default=None, help="process only first N tickers")
    parser.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip", "none"])
    parser.add_argument("--no-hash", action="store_true", help="skip sha256 (faster provenance)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    input_dir = Path(args.input)
    if not input_dir.is_absolute():
        input_dir = ROOT / input_dir
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    if not input_dir.exists():
        log.error("Input directory not found: %s", input_dir)
        return 2

    summary = run_extraction(
        input_dir,
        output,
        tickers=args.tickers,
        limit=args.limit,
        compression=args.compression,
        compute_hash=not args.no_hash,
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "errors"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
