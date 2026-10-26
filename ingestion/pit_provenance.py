"""PIT provenance helpers for SEC XBRL fundamentals artifacts.

Shared by the full XBRL extractor, the US-GAAP concept mapper, the dense
fundamental builder, and the coverage audit.  Every parquet written by this
track gets a sidecar ``<path>.provenance.json`` matching the Data_Strategy
section 8 contract, plus a row in ``data/provenance/manifest.jsonl``.

Contract keys (house standard):
    source, retrieval_timestamp, source_version, pit_timestamp_column,
    entity_key, transformations, sha256, row_count, date_range,
    mapping_confidence  (+ optional caller-supplied ``extra`` keys)

The helpers here are deliberately dependency-light (pyarrow + stdlib) so they
can run inside a streaming extraction loop without importing pandas.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

CHUNK = 1 << 20  # 1 MiB


def utc_now_iso() -> str:
    """Return a tz-aware UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Streaming SHA-256 of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def write_provenance(
    artifact_path: str | os.PathLike[str],
    *,
    source: str,
    source_version: str,
    pit_timestamp_column: str,
    entity_key: str,
    transformations: Iterable[str],
    row_count: int,
    date_range: Optional[Dict[str, Optional[str]]] = None,
    mapping_confidence: str = "high",
    retrieval_timestamp: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    manifest_path: Optional[str | os.PathLike[str]] = None,
    compute_sha256: bool = True,
) -> Dict[str, Any]:
    """Write a ``.provenance.json`` sidecar for a parquet/data artifact.

    Parameters
    ----------
    artifact_path:
        File the provenance describes.  The sidecar is
        ``<artifact_path>.provenance.json``.
    source_version:
        e.g. ``companyfacts_20261010``.
    pit_timestamp_column:
        Column that anchors point-in-time availability (``filed`` /
        ``filed_date``).
    transformations:
        Ordered list of transformation/step names.
    date_range:
        ``{"min": ..., "max": ...}`` typically derived from the PIT column.
    extra:
        Additional provenance keys merged at the top level.

    Returns
    -------
    The provenance dict that was serialised.
    """
    artifact = Path(artifact_path)
    prov: Dict[str, Any] = {
        "source": source,
        "retrieval_timestamp": retrieval_timestamp or utc_now_iso(),
        "source_version": source_version,
        "pit_timestamp_column": pit_timestamp_column,
        "entity_key": entity_key,
        "transformations": list(transformations),
        "row_count": int(row_count),
        "date_range": date_range or {"min": None, "max": None},
        "mapping_confidence": mapping_confidence,
    }
    if compute_sha256 and artifact.exists():
        prov["sha256"] = sha256_file(artifact)
    else:
        prov["sha256"] = None
    if extra:
        for k, v in extra.items():
            prov.setdefault(k, v)

    sidecar = Path(str(artifact) + ".provenance.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(prov, f, indent=2, default=str)

    # Append to the shared manifest (best-effort, never fatal).
    mpath = Path(manifest_path) if manifest_path else (
        Path(__file__).resolve().parents[1] / "data" / "provenance" / "manifest.jsonl"
    )
    try:
        mpath.parent.mkdir(parents=True, exist_ok=True)
        record = dict(prov)
        record["artifact"] = str(artifact).replace("\\", "/")
        with open(mpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass

    return prov
