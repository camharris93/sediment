"""L5 — Guarded execution.

The BigQuery original guarded against a cost-bomb (maximum_bytes_billed). DuckDB
is a local file with no per-query cost, so the cost gate is gone; the guards that
remain are the ones that still matter for trust:
  • read-only — reject any non-SELECT/CTE/UNION statement defensively.
  • row cap   — bound the result set so a careless query can't flood the console.
Full provenance (exact SQL, elapsed, row count, truncation) is captured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from .dry_run import DryRunReport
from .warehouse import DuckDBWarehouse

DIALECT = "duckdb"
MAX_RESULT_ROWS = 1000
QUERY_TIMEOUT_SEC = 60

RefusalKind = Literal["non_read_only", "dry_run_not_passed", "execution_error"]


@dataclass
class Provenance:
    sql: str
    elapsed_ms: int
    row_count: int
    truncated: bool
    total_rows_returned: int


@dataclass
class Refusal:
    kind: RefusalKind
    message: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "detail": self.detail}


@dataclass
class ExecutionOutcome:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    provenance: Provenance | None = None
    refusal: Refusal | None = None


# DDL/DML and anything sqlglot models as a non-query statement. `Command` is the
# catch-all for things sqlglot doesn't model structurally (PRAGMA, CALL, EXPORT…).
# `Copy` writes to the filesystem; `Attach`/`Detach`/`Set`/`Pragma`/`Use` change
# engine state; `Install`/`LoadData` pull in extensions (network exfil surface).
# DuckDB's read-only connection blocks the writes but NOT Copy/Install/Set/Pragma —
# so they must be denied here, in the layer that owns the trust boundary.
_DENIED = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Merge, exp.Command, exp.Copy, exp.Attach, exp.Detach,
    exp.Set, exp.Pragma, exp.Use, exp.Install, exp.LoadData,
)

# Only these may sit at the top of an accepted statement.
_QUERY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery, exp.With)

# A FROM source must be a NAMED warehouse relation (`schema.table`) or a CTE — i.e.
# a Table node whose `this` is a plain identifier. Every DuckDB file/network reader
# (read_csv, read_parquet, read_json, read_text, glob, delta_scan, …) instead parses
# as a Table WRAPPING A FUNCTION. So rather than chase a denylist of reader names
# (which grows with every DuckDB release), we reject any function-valued FROM source
# structurally — that catches today's readers and tomorrow's. A short allowlist of
# pure, side-effect-free generators stays permitted so ordinary analytical SQL works.
_SAFE_TABLE_FUNCS = frozenset({"generate_series", "range", "unnest", "values"})


def _external_source_name(node: exp.Expression) -> str | None:
    """If `node` is a FROM source backed by a table-valued FUNCTION (a file/network
    reader or other non-relation source), return its lowercased name; else None.
    A normal `schema.table` reference has `this` as an Identifier and returns None."""
    if not isinstance(node, exp.Table):
        return None
    inner = node.this
    if inner is None or isinstance(inner, exp.Identifier):
        return None  # ordinary named relation
    if isinstance(inner, exp.Func):
        # Anonymous funcs carry the real name in `.name`; typed reader nodes
        # (ReadCSV, ReadParquet, …) carry their arg there, so use the class name.
        nm = (inner.name if isinstance(inner, exp.Anonymous) else inner.sql_name()).lower()
        if nm in _SAFE_TABLE_FUNCS:
            return None
        return nm or "function"
    return None


def _is_read_only(sql: str) -> tuple[bool, str | None]:
    # Parse ALL statements: a single `;`-joined payload (e.g. `SELECT 1; DROP …`)
    # must be rejected wholesale, not have only its first statement inspected.
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except ParseError as exc:
        return False, f"could not parse SQL for read-only check: {exc}"
    statements = [s for s in statements if s is not None]
    if not statements:
        return False, "empty AST"
    if len(statements) > 1:
        return False, f"multiple statements are not permitted ({len(statements)} found)"

    ast = statements[0]
    if not isinstance(ast, _QUERY_ROOTS):
        return False, f"non-query top-level statement: {type(ast).__name__.upper()}"
    for n in ast.walk():
        if isinstance(n, _DENIED):
            return False, f"statement contains a {type(n).__name__.upper()} which is not permitted"
        ext = _external_source_name(n)
        if ext:
            return False, (f"statement reads from an external source `{ext}(...)` — only "
                           "curated warehouse relations may be queried")
    return True, None


def is_read_only_query(sql: str) -> tuple[bool, str | None]:
    """Public form of the L5 read-only gate: True iff `sql` is a single read-only
    query touching only warehouse relations (no writes, no multi-statement, no
    external-source table functions). Reused by the build path (engine/modeling)
    so chat SQL is gated identically before it is ever run read-WRITE."""
    return _is_read_only(sql)


def guarded_execute(sql: str, dry_run_report: DryRunReport, *,
                    warehouse: DuckDBWarehouse | None = None) -> ExecutionOutcome:
    if not dry_run_report.ok:
        return ExecutionOutcome(False, refusal=Refusal(
            "dry_run_not_passed", "dry-run did not pass; refusing to execute",
            detail=dry_run_report.summary or dry_run_report.error))

    ok, reason = _is_read_only(sql)
    if not ok:
        return ExecutionOutcome(False, refusal=Refusal("non_read_only", reason or "not read-only"))

    wh = warehouse or DuckDBWarehouse()
    try:
        result = wh.execute(sql, max_result_rows=MAX_RESULT_ROWS, timeout_sec=QUERY_TIMEOUT_SEC)
    except Exception as exc:
        return ExecutionOutcome(False, refusal=Refusal(
            "execution_error", "DuckDB execution failed", detail=str(exc)))

    prov = Provenance(sql=sql, elapsed_ms=result.elapsed_ms, row_count=len(result.rows),
                      truncated=result.truncated, total_rows_returned=result.total_rows_returned)
    return ExecutionOutcome(True, rows=result.rows, provenance=prov)
