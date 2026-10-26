"""
test_merged_universe_persistence.py -- focused tests for the P0A persistence layer.

Scope is deliberately the hard parts, not the happy path: partition-append
safety, chain-order enforcement, provenance tamper-evidence, the fallback
log's provenance linkage, and the read paths (DuckDB and the pyarrow fallback).

Run:  python -m pytest tests/test_merged_universe_persistence.py -q
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.persistence.merged_universe import (  # noqa: E402
    FALLBACK_TRACKING_SCHEMA,
    MERGED_UNIVERSE_SCHEMA,
    TIER_FILED,
    TIER_FORWARD,
    TIER_SECTOR_MEDIAN,
    TRACKED_FIELDS,
    GENESIS,
    MergedUniverseStore,
    SchemaContractError,
    _partition_relpaths,
    _read_partition,
    assign_provenance,
    build_merged_pit_universe,
    canonical_row_payload,
    compute_row_sha256,
    make_uuid7,
    normalize_cap_bucket,
    schema_contract,
)
from ingestion.trade_audit import canonical_json  # noqa: E402

RECON = Path("data/universe/monthly_reconstitution.parquet")
FUND = Path("data/pit_fundamentals/valuation_fundamentals.parquet")

pytestmark = pytest.mark.skipif(
    not (RECON.exists() and FUND.exists()),
    reason="needs data/universe/monthly_reconstitution.parquet + valuation_fundamentals.parquet",
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def months():
    r = pd.read_parquet(RECON, columns=["rebalance_date"])
    return sorted(pd.to_datetime(r["rebalance_date"]).unique())


@pytest.fixture(scope="module")
def built_by_month(months, tmp_path_factory):
    """Per-month reference-producer output: {iso_month: (merged, fallback)}.

    ``build_merged_pit_universe`` reads from the source of record (paths), so
    each slice is staged to parquet first -- this also exercises the same code
    path production uses, and lets append/chronology tests use frames whose
    filed_date values are genuinely consistent with their rebalance_date.
    """
    tmp = tmp_path_factory.mktemp("inputs")
    recon = pd.read_parquet(RECON)
    recon["rebalance_date"] = pd.to_datetime(recon["rebalance_date"])
    fund = pd.read_parquet(FUND)
    fund["filed_date"] = pd.to_datetime(fund["filed_date"])
    out = {}
    for m in months[:3]:
        rp, fp = tmp / f"recon_{m:%Y%m}.parquet", tmp / f"fund_{m:%Y%m}.parquet"
        r = recon[recon["rebalance_date"] == m].copy()
        r.to_parquet(rp, index=False)
        fund[fund["filed_date"] <= pd.Timestamp(m)].to_parquet(fp, index=False)
        merged, fb = build_merged_pit_universe(rp, fp)
        out[f"{m:%Y-%m}"] = (merged, fb)
    return out


@pytest.fixture(scope="module")
def built(built_by_month):
    """First two rebalance months concatenated (the common case)."""
    a = built_by_month[list(built_by_month)[0]]
    b = built_by_month[list(built_by_month)[1]]
    return (pd.concat([a[0], b[0]], ignore_index=True),
            pd.concat([a[1], b[1]], ignore_index=True))


@pytest.fixture
def store(tmp_path):
    return MergedUniverseStore(root=tmp_path / "u", backtest_root=tmp_path / "bt")


# --------------------------------------------------------------------------
# schema contract
# --------------------------------------------------------------------------
def test_schema_contract_chain_order_is_serializable():
    """chain_order is built from a tuple+list concat -- must not TypeError."""
    payload = schema_contract(MERGED_UNIVERSE_SCHEMA, "merged_pit_universe")
    assert payload["chain_order"] == ["rebalance_date", "cap_bucket", "ticker"]
    assert payload["field_count"] == 33
    assert len(payload["fields"]) == 33


def test_frozen_column_count():
    assert len(MERGED_UNIVERSE_SCHEMA.names) == 33
    assert len(FALLBACK_TRACKING_SCHEMA.names) == 10
    # provenance columns are non-nullable and excluded from the row hash
    for name in ("sha256", "uuid", "timestamp_ns"):
        f = MERGED_UNIVERSE_SCHEMA.field(name)
        assert not f.nullable, f"{name} must be non-nullable"


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------
def test_chain_is_canonical_order_invariant(built):
    """Row hashes depend on canonical order, not on arrival order.

    ``assign_provenance`` sorts into ``(rebalance_date, cap_bucket, ticker)``
    before chaining, so a shuffled input must reproduce the *same* hash list.
    That is strictly stronger than order-sensitivity: the chain is a function of
    the data, so a rebuild is byte-reproducible.
    """
    df = built[0]
    a, ha = assign_provenance(df.copy(), prev_chain_hash=GENESIS, retrieval_ns=1)
    b, hb = assign_provenance(df.sample(frac=1.0, random_state=7).reset_index(drop=True),
                              prev_chain_hash=GENESIS, retrieval_ns=1)
    assert a["sha256"].tolist() == b["sha256"].tolist()
    assert ha == hb
    # and the stored order really is canonical
    stored = a
    assert stored["rebalance_date"].is_monotonic_increasing


def test_prev_chain_hash_changes_every_downstream_hash(built):
    """Linkage is real: a different genesis changes every link."""
    df = built[0]
    _, h_gen = assign_provenance(df.copy(), prev_chain_hash=GENESIS, retrieval_ns=1)
    _, h_alt = assign_provenance(df.copy(), prev_chain_hash="f" * 64, retrieval_ns=1)
    assert h_gen != h_alt
    a, _ = assign_provenance(df.copy(), prev_chain_hash=GENESIS, retrieval_ns=1)
    b, _ = assign_provenance(df.copy(), prev_chain_hash="f" * 64, retrieval_ns=1)
    assert a["sha256"].tolist() != b["sha256"].tolist()


def test_uuid_is_deterministic_from_content(built):
    df = built[0]
    stamped, _ = assign_provenance(df.copy(), prev_chain_hash=GENESIS, retrieval_ns=1)
    row = stamped.iloc[0].to_dict()
    payload = canonical_row_payload(row)
    assert row["uuid"] == make_uuid7(int(row["timestamp_ns"]),
                                     seed=canonical_json([payload, row["sha256"]]))


def test_row_hash_excludes_provenance_columns(built):
    df = built[0]
    stamped, _ = assign_provenance(df.copy(), prev_chain_hash=GENESIS, retrieval_ns=1)
    payload = canonical_row_payload(stamped.iloc[0].to_dict())
    assert not {"sha256", "uuid", "timestamp_ns"} & set(payload)


# --------------------------------------------------------------------------
# store: write / verify
# --------------------------------------------------------------------------
def test_write_merged_roundtrip_and_chain(store, built):
    merged, fb = built
    r = store.write_merged(merged)
    assert r.rows_written == len(merged)
    assert r.chain_head != GENESIS
    assert store.merged_path.is_dir(), "dataset root must be a directory"
    v = store.verify_merged_chain(verify_hashes=True, recompute_sources=True)
    assert v["intact"], v
    assert v["rows"] == len(merged)


def test_written_partitions_match_frozen_schema(store, built):
    store.write_merged(built[0])
    for keys, p in _partition_relpaths(store.merged_path):
        assert pq.ParquetFile(str(p)).schema_arrow.equals(MERGED_UNIVERSE_SCHEMA)
        assert keys[0].startswith("20")          # rebalance_date=YYYY-MM-DD
        assert keys[1] in {"large", "mid", "micro"}


def test_tamper_breaks_chain(store, built):
    store.write_merged(built[0])
    p = sorted(store.merged_path.rglob("*.parquet"))[0]
    df = _read_partition(p)
    df.loc[0, "wacc"] = float(df.loc[0, "wacc"]) + 0.01
    df.to_parquet(p, index=False)
    v = store.verify_merged_chain(verify_hashes=True)
    assert not v["intact"]
    assert v["first_break"] is not None


def test_append_is_refused_for_duplicate_keys(store, built_by_month):
    """Rewriting a month already on disk is refused.

    Uses the same month twice so the *duplicate-key* guard is the one under
    test, not the chain-order guard (which legitimately fires first when an
    earlier month is appended to a later one).
    """
    jan = built_by_month[list(built_by_month)[0]][0]
    feb = built_by_month[list(built_by_month)[1]][0]
    store.write_merged(jan)                       # head -> Jan
    with pytest.raises(SchemaContractError, match="refusing to rewrite"):
        store.write_merged(pd.concat([jan, feb], ignore_index=True))


def test_append_of_earlier_date_is_refused(store, built_by_month):
    """Appending a partition that sorts before the chain head would reorder the
    chain and invalidate every stored hash -- must be refused.

    Written Feb-first then Jan: Jan is a genuinely valid frame (built from its
    own PIT gate), so this isolates the chronology guard from the PIT check.
    """
    jan = built_by_month[list(built_by_month)[0]][0]
    feb = built_by_month[list(built_by_month)[1]][0]
    store.write_merged(feb)                       # head -> Feb
    with pytest.raises(SchemaContractError, match="break the SHA chain order"):
        store.write_merged(jan)


def test_chronological_append_succeeds_and_extends_chain(store, built_by_month):
    jan = built_by_month[list(built_by_month)[0]][0]
    feb = built_by_month[list(built_by_month)[1]][0]
    r1 = store.write_merged(jan)
    r2 = store.write_merged(feb)
    assert r2.chain_head != r1.chain_head
    assert store.verify_merged_chain(verify_hashes=True, recompute_sources=True)["intact"]
    total = sum(len(_read_partition(p)) for _, p in _partition_relpaths(store.merged_path))
    assert total == len(jan) + len(feb)


def test_stale_file_at_dataset_path_is_reported(store, built):
    store.merged_path.parent.mkdir(parents=True, exist_ok=True)
    store.merged_path.write_bytes(b"not a dataset")
    with pytest.raises(SchemaContractError, match="exists as a FILE"):
        store.write_merged(built[0])
    # ...and --replace clears it
    r = store.write_merged(built[0], allow_replace=True)
    assert r.rows_written > 0
    assert store.merged_path.is_dir()


def test_partition_append_preserves_prior_rows(store, built):
    """Writing one month twice into the same partition must not drop rows."""
    merged = built[0]
    store.write_merged(merged)
    store.write_merged(merged, allow_replace=True)
    total = sum(len(_read_partition(p)) for _, p in _partition_relpaths(store.merged_path))
    assert total == len(merged), f"expected {len(merged)} rows, found {total}"


def test_fallback_append_preserves_every_component_event(store, built_by_month):
    """Regression: the fallback entity key is `(date, ticker, field)`.

    One ticker legitimately carries several fallback rows per month -- one per
    component (`wacc`, `wacc.cost_of_debt`, `icr`,
    `icr.interest_expense`, `roic`).  An append merge keyed on
    `(rebalance_date, ticker)` keeps only the last of them and silently
    destroys the rest, while the row count of a *fresh* write still looks
    perfectly healthy -- so only a rewrite/append path can catch it.
    """
    months = sorted(built_by_month)
    m1, m2, m3 = months[0], months[1], months[2]
    first_merged = pd.concat([built_by_month[m1][0], built_by_month[m2][0]],
                              ignore_index=True)
    first_fb = pd.concat([built_by_month[m1][1], built_by_month[m2][1]],
                         ignore_index=True)
    store.write_merged(first_merged)
    store.write_fallback_tracking(first_fb)
    assert len(store.read_fallback_tracking()) == len(first_fb)

    # (1) idempotent rewrite: identical content must not change the row count
    store.write_fallback_tracking(first_fb)
    assert len(store.read_fallback_tracking()) == len(first_fb), \
        "idempotent rewrite changed the fallback row count"

    # (2) incremental append of a real third month: must add, not replace
    third_merged, third_fb = built_by_month[m3]
    store.write_merged(third_merged)
    store.write_fallback_tracking(third_fb)
    final = store.read_fallback_tracking()
    assert len(final) == len(first_fb) + len(third_fb), \
        "incremental append lost component events"

    # (3) the stored logical key set must equal exactly what the producer
    # emitted -- no collapsed components, no invented rows, no dupes
    expected = pd.concat([first_fb, third_fb], ignore_index=True)
    expected["rebalance_date"] = pd.to_datetime(expected["rebalance_date"]).dt.strftime("%Y-%m-%d")
    final["rebalance_date"] = pd.to_datetime(final["rebalance_date"]).dt.strftime("%Y-%m-%d")
    got_key = set(map(tuple, final[["rebalance_date", "ticker", "field"]].values))
    want_key = set(map(tuple, expected[["rebalance_date", "ticker", "field"]].values))
    assert len(got_key) == len(final), "duplicate (date, ticker, field) rows on disk"
    assert got_key == want_key, (
        f"stored fallback events differ from producer output: "
        f"missing={len(want_key - got_key)} extra={len(got_key - want_key)}")
    assert final.groupby(["rebalance_date", "ticker"]).size().max() > 1, \
        "fixture has only one event per ticker; test cannot detect collapse"


# --------------------------------------------------------------------------
# fallback_tracking
# --------------------------------------------------------------------------
def test_fallback_tracking_adopts_merged_provenance(store, built):
    merged, fb = built
    store.write_merged(merged)
    store.write_fallback_tracking(fb)
    out = store.read_fallback_tracking()
    assert len(out) == len(fb)
    for c in ("provenance_sha256", "provenance_uuid", "provenance_timestamp_ns"):
        assert out[c].notna().all(), f"{c} must be fully populated"
    # every event must point at a real merged row's chain hash
    merged_hashes = set(store.read_merged("0001-01-01", "9999-12-31")["sha256"])
    assert set(out["provenance_sha256"]) <= merged_hashes


def test_fallback_tracking_requires_merged_first(store, built):
    with pytest.raises(SchemaContractError, match="write_merged"):
        store.write_fallback_tracking(built[1])


def test_orphan_fallback_event_is_rejected(store, built):
    merged, fb = built
    store.write_merged(merged)
    orphan = fb.copy()
    orphan.loc[orphan.index[0], "ticker"] = "ZZZZNOTREAL"
    with pytest.raises(SchemaContractError, match="absent from merged_pit_universe"):
        store.write_fallback_tracking(orphan)


# --------------------------------------------------------------------------
# read paths
# --------------------------------------------------------------------------
def test_read_merged_window_and_filter(store, built):
    store.write_merged(built[0])
    full = store.read_merged("0001-01-01", "9999-12-31")
    assert len(full) == len(built[0])
    one = store.read_merged("2018-01-31", "2018-01-31")
    assert len(one) == int((pd.to_datetime(full["rebalance_date"]).dt.strftime("%Y-%m-%d")
                            == "2018-01-31").sum())
    tickers = sorted(full["ticker"].unique())[:5]
    sub = store.read_merged("0001-01-01", "9999-12-31", tickers=tickers)
    assert set(sub["ticker"]) == set(tickers)


def test_pyarrow_fallback_matches_duckdb(store, built):
    store.write_merged(built[0])
    cols = ["ticker", "rebalance_date", "cap_bucket", "wacc", "wacc_fallback_tier"]
    lo, hi = "0001-01-01", "9999-12-31"
    duck = store._duckdb_read(store.merged_path, cols, lo, hi, None)
    arrow = store._pyarrow_read(store.merged_path, cols, lo, hi, None)
    assert duck is not None, "DuckDB is the primary read path and must not silently fail"
    assert len(duck) == len(arrow)
    duck = duck.sort_values(cols[:2]).reset_index(drop=True)
    arrow = arrow.sort_values(cols[:2]).reset_index(drop=True)
    assert duck["ticker"].tolist() == arrow["ticker"].tolist()
    assert duck["rebalance_date"].tolist() == arrow["rebalance_date"].tolist()
    # both engines must agree on the payload too, not just the keys
    assert duck["wacc"].round(12).tolist() == arrow["wacc"].round(12).tolist()


# --------------------------------------------------------------------------
# producer semantics
# --------------------------------------------------------------------------
def test_cap_bucket_normalization():
    assert normalize_cap_bucket("Mega") == "large"
    assert normalize_cap_bucket("mega") == "large"
    assert normalize_cap_bucket("Large") == "mid"
    assert normalize_cap_bucket("Mid") == "mid"
    assert normalize_cap_bucket("Small", market_cap=1e9) == "micro"


def test_no_lookahead_in_produced_output(built):
    df = built[0]
    for fld in TRACKED_FIELDS:
        fdcol = f"{fld}_filed_date"
        if fdcol not in df.columns:
            # sector comes from the reconstitution, not from a filing, so it has
            # no filed_date by design (and always sits at tier 0).
            assert fld == "sector", f"{fld} has no filed_date column"
            assert (df["sector_fallback_tier"] == TIER_FILED).all()
            continue
        fd, rd, tier = df[fdcol], df["rebalance_date"], df[f"{fld}_fallback_tier"]
        m = fd.notna() & rd.notna()
        assert not (fd[m] > rd[m]).any(), f"{fld} contains lookahead"
        # tier 0/1 came from a filing, so it MUST carry that filing's date;
        # tier >= 2 is a cross-sectional fallback with no single filing, so it
        # MUST NOT invent one.
        assert df.loc[tier <= TIER_FORWARD, fdcol].notna().all(), \
            f"{fld}: a filed value without a filed_date"
        assert df.loc[tier >= TIER_SECTOR_MEDIAN, fdcol].isna().all(), \
            f"{fld}: a fallback value claiming a filed_date"


def test_sector_never_uses_constant_fallback(built):
    df = built[0]
    assert int((df["sector_fallback_tier"] == 3).sum()) == 0


def test_fallback_distribution_shape(store, built):
    store.write_merged(built[0])
    dist = store.fallback_distribution()
    assert set(dist["field"]) == set(TRACKED_FIELDS)
    assert (dist["tier_3_frac"] == 0).all()


# --------------------------------------------------------------------------
# ticker_usage
# --------------------------------------------------------------------------
def test_write_and_read_ticker_usage(store, built):
    """ticker_usage carries the frozen 11-column contract + provenance.

    Provenance is *adopted from the merged row* the arm selected, so the merged
    dataset must exist first -- which is the point: an Arm's selection is only
    auditable if it can be traced back to a hash-linked input row.
    """
    store.write_merged(built[0])
    # pick tickers that really are in the month's universe -- a usage row for a
    # ticker outside it is an orphan and must be refused (see the guard test below)
    jan = built[0][pd.to_datetime(built[0]["rebalance_date"]).dt.strftime("%Y-%m")
                  == "2018-01"]
    assert len(jan) > 0, f"fixture has no 2018-01 rows; dates={sorted(pd.to_datetime(built[0]['rebalance_date']).unique())[:4]}"
    tickers = sorted(jan["ticker"].astype(str))[:2]
    n = len(tickers)
    df = pd.DataFrame({
        "ticker": tickers,
        "rebalance_date": [pd.Timestamp("2018-01-31")] * n,
        "arm": ["A"] * n,
        "weight": list(np.linspace(0.6, 0.4, n)),
        "wacc_fallback_tier": [0, 2][:n],
        "icr_fallback_tier": [1, 2][:n],
        "sector_fallback_tier": [0, 0][:n],
        "any_fallback": [False, True][:n],
    })
    r = store.write_ticker_usage("A", "2018_2024", df)
    assert r.rows_written == n
    out = store.read_ticker_usage(arm="A", window="2018_2024")
    assert len(out) == n, (
        f"read returned {len(out)} rows; path exists="
        f"{store.usage_path('A', '2018_2024').exists()}, "
        f"parts={[k for k, _ in _partition_relpaths(store.usage_path('A', '2018_2024'))]}")
    assert set(out["arm"]) == {"a"}, f"arm={set(out['arm'])}"
    for c in ("provenance_sha256", "provenance_uuid", "provenance_timestamp_ns"):
        assert out[c].notna().all(), f"{c} must be stamped by write_ticker_usage"
    # provenance points at a real merged row
    merged_hashes = set(store.read_merged("0001-01-01", "9999-12-31")["sha256"])
    assert set(out["provenance_sha256"]) <= merged_hashes
    # partition layout: rebalance_date=.../arm=A/part-0000.parquet
    keys = [k for k, _ in _partition_relpaths(store.usage_path("A", "2018_2024"))]
    assert ("2018-01-31", "a") in keys, f"partition keys are lowercased: {keys}"
    assert (store.usage_path("A", "2018_2024") / "_manifest.json").exists()


def test_write_ticker_usage_requires_merged(store):
    """Guard: usage rows cannot be persisted without hash-linked merged rows."""
    df = pd.DataFrame({
        "ticker": ["MSFT"], "rebalance_date": [pd.Timestamp("2018-01-31")],
        "arm": ["A"], "weight": [1.0], "wacc_fallback_tier": [0],
        "icr_fallback_tier": [0], "sector_fallback_tier": [0], "any_fallback": [False],
    })
    with pytest.raises(SchemaContractError, match="write_merged"):
        store.write_ticker_usage("A", "2018_2024", df)


def test_read_ticker_usage_accepts_legacy_flat_file(store):
    """Workstream A's runner writes one FLAT file per (arm, window); the reader
    must accept that layout so 233 existing artifacts stay usable."""
    df = pd.DataFrame({
        "ticker": ["AAA"], "rebalance_date": [pd.Timestamp("2018-02-01")],
        "arm": ["A"], "weight": [1.0], "wacc_fallback_tier": [0],
        "icr_fallback_tier": [0], "sector_fallback_tier": [0], "any_fallback": [False],
        "provenance_sha256": ["a" * 64], "provenance_uuid": ["u"],
        "provenance_timestamp_ns": [np.int64(1)],
    })
    flat = store.usage_path("A", "2018-02-01")
    flat.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(flat, index=False)          # flat file, not a directory
    out = store.read_ticker_usage(arm="A", window="2018-02-01")
    assert len(out) == 1
    assert out["ticker"].tolist() == ["AAA"]


def test_write_refuses_to_silently_replace_a_legacy_flat_file(store):
    """The migration decision must be explicit, not made by a write."""
    flat = store.usage_path("A", "2018-02-01")
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"legacy flat file")
    df = pd.DataFrame({
        "ticker": ["AAA"], "rebalance_date": [pd.Timestamp("2018-02-01")],
        "arm": ["A"], "weight": [1.0], "wacc_fallback_tier": [0],
        "icr_fallback_tier": [0], "sector_fallback_tier": [0], "any_fallback": [False],
    })
    with pytest.raises(SchemaContractError, match="exists as a FILE"):
        store.write_ticker_usage("A", "2018-02-01", df)


def test_orphan_ticker_usage_row_is_rejected(store, built):
    """A usage row naming a ticker outside the universe has no merged row to
    hash-link to, so it must be refused rather than written unlinked."""
    store.write_merged(built[0])
    df = pd.DataFrame({
        "ticker": ["ZZZZNOTREAL"], "rebalance_date": [pd.Timestamp("2018-01-31")],
        "arm": ["A"], "weight": [1.0], "wacc_fallback_tier": [0],
        "icr_fallback_tier": [0], "sector_fallback_tier": [0], "any_fallback": [False],
    })
    with pytest.raises(SchemaContractError, match="absent from merged_pit_universe"):
        store.write_ticker_usage("A", "2018_2024", df)