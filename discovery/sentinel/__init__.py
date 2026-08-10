"""Sentinel — the real-time discovery funnel (B-20260809-003).

Queue-first, DB-first (WAL), fail-closed gates:

  G0 dedup/entity -> G1 survival & solvency -> G2 fundamentals
  -> G3 alt-data (reddit/github) -> G4 web enrichment (advisory)

Sources stay isolated lanes; a shared SQLite token-bucket governor +
per-lane circuit breakers enforce safety over speed. No media is ever
persisted: IG audio/video is transcribed and dropped.
"""
