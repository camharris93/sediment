"""Warehouse seam — DuckDB implementation.

The sql-engine original was BigQuery, where a `dry_run` returns bytes-that-WOULD-
be-billed. DuckDB is a local file: there is no cloud, no per-query cost. So:

  • dry_run  -> `EXPLAIN <sql>`: DuckDB binds and plans the query (authoritative
                semantic check — catches bad columns/types/functions) without
                executing it. `bytes_processed` is reported as 0 (not meaningful).
  • execute  -> read-only connection, fetch up to a row cap, capture timing.

Same Protocol shape as the original so the rest of the pipeline is unchanged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from ..config import WAREHOUSE_PATH


@dataclass
class DryRunResult:
    ok: bool
    bytes_processed: int = 0
    error: str | None = None


@dataclass
class ExecutionResult:
    rows: list[dict[str, Any]]
    elapsed_ms: int
    truncated: bool = False
    total_rows_returned: int = 0


class DuckDBWarehouse:
    def __init__(self, path: Path = WAREHOUSE_PATH) -> None:
        self._path = str(path)

    def _connect(self):
        # Read-only: the query agent must never mutate the warehouse, and this
        # lets it run while other readers (the dashboard) are connected.
        return duckdb.connect(self._path, read_only=True)

    def dry_run(self, sql: str) -> DryRunResult:
        try:
            con = self._connect()
            try:
                con.execute("EXPLAIN " + sql)
            finally:
                con.close()
            return DryRunResult(ok=True, bytes_processed=0)
        except Exception as exc:
            return DryRunResult(ok=False, error=str(exc))

    def execute(self, sql: str, *, max_result_rows: int | None = None,
                timeout_sec: int = 60) -> ExecutionResult:
        con = self._connect()
        t0 = time.time()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            if max_result_rows is None:
                fetched = cur.fetchall()
                truncated = False
            else:
                fetched = cur.fetchmany(max_result_rows + 1)
                truncated = len(fetched) > max_result_rows
                if truncated:
                    fetched = fetched[:max_result_rows]
            rows = [dict(zip(cols, r)) for r in fetched]
        finally:
            con.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        return ExecutionResult(
            rows=rows, elapsed_ms=elapsed_ms,
            truncated=truncated, total_rows_returned=len(rows),
        )
