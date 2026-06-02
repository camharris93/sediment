"""NL->SQL query agent — the consumption edge.

Ported from the sibling sql-engine project (a trust-first, layered NL->SQL agent)
and adapted to the local DuckDB warehouse. The layered architecture is preserved:

  L0  grounding      — schema/grain/profile pulled DYNAMICALLY from DuckDB, so it
                       adapts to whatever marts exist for any dataset.
  L1  intent         — restate the question + surface assumptions (auditable).
  L2  generation     — constrained DuckDB SQL over the grounded schema only.
  L3  static check   — sqlglot validates columns/tables WITHOUT touching the db.
  L4  dry-run        — DuckDB EXPLAIN: authoritative bind/semantic check, no exec.
  L5  execution      — read-only guard + row cap, full provenance.
  L6  plausibility   — fan-out + result-value sanity checks.
  L7  translation    — plain-English answer + trust badge; every number traces
                       to the visible SQL.

The two BigQuery-specific seams are swapped for DuckDB: the warehouse (no cloud,
no bytes-billed cost model) and grounding (information_schema over local marts).
"""
