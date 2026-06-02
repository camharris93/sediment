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


_DENIED = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
           exp.TruncateTable, exp.Merge, exp.Command)


def _is_read_only(sql: str) -> tuple[bool, str | None]:
    try:
        ast = sqlglot.parse_one(sql, dialect=DIALECT)
    except ParseError as exc:
        return False, f"could not parse SQL for read-only check: {exc}"
    if ast is None:
        return False, "empty AST"
    if not isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery, exp.With)):
        return False, f"non-query top-level statement: {type(ast).__name__.upper()}"
    for n in ast.walk():
        if isinstance(n, _DENIED):
            return False, f"statement contains a {type(n).__name__.upper()} which is not permitted"
    return True, None


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
