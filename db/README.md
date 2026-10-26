# `db/` — SQLite Layer (connection, schema, feature/pit stores)

## Roadmap Position
Pipeline stage 1 (persistence): the SQLite access layer. `connection.py:6` sets
`DB_PATH = "reddit_quant.db"` — **the root-level `reddit_quant.db` (203 MB)
exists because this module points at it; moving the DB requires changing this
line first (NEEDS_RULING — see root README).**

## Folder Summary
`connection.py` is the connection factory; `schema*.py` files define the schema
families (main, consensus, discovery, fintech, PIT); `feature_store.py` and
`pit_reader.py` read/write features and PIT data; `jobs.py` tracks job state.

## Skeleton
```
db/
  __init__.py connection.py schema.py schema_consensus.py schema_discovery.py
  schema_fintech.py schema_pit.py feature_store.py jobs.py pit_reader.py
```

## Files

| File | Purpose |
|---|---|
| `connection.py` | connection factory; `DB_PATH = "reddit_quant.db"` (root) |
| `schema*.py` | schema families (main/consensus/discovery/fintech/pit) |
| `feature_store.py` | feature persistence |
| `pit_reader.py` | PIT data access |
| `jobs.py` | job state tracking |

## Entrypoints
`python -m db.connection --check` (exact CLI per module docstring).

## Dependencies
Root `reddit_quant.db` (~40 modules reference it via `db/connection.py`).

## Efficient Pathways
- `universe_build`: `pit_reader.py` is the PIT access path for universe builds
- `full_pipeline`: schema changes must be additive; the DB is live for reddit
  signal backtesting (`scripts/check_reddit_db.py`, `backtest_reddit_signal.py`)