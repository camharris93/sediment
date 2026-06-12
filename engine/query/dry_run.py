"""L4 — Dry-run validation via DuckDB EXPLAIN.

The BigQuery original used a billed dry-run; DuckDB's `EXPLAIN` binds and plans
the query (an authoritative semantic check the static layer can't fully match —
real function signatures, type coercions, group-by validity) without executing a
single row. A failure feeds DuckDB's own error text back into the L2 loop.
"""
from __future__ import annotations

from dataclasses import dataclass

from .warehouse import DuckDBWarehouse


@dataclass
class DryRunReport:
    ok: bool
    bytes_processed: int = 0
    error: str | None = None
    summary: str | None = None

    def to_feedback_line(self) -> str:
        if self.ok:
            return "L4 EXPLAIN passed."
        return f"L4 EXPLAIN FAILED: {self.summary or self.error}"


def _summarize(text: str) -> str:
    text = (text or "").strip()
    # DuckDB errors are usually a single informative line; keep the first 2.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(lines[:2]) if lines else text


def dry_run_check(sql: str, warehouse: DuckDBWarehouse | None = None) -> DryRunReport:
    wh = warehouse or DuckDBWarehouse()
    result = wh.dry_run(sql)
    if result.ok:
        return DryRunReport(ok=True)
    return DryRunReport(ok=False, error=result.error, summary=_summarize(result.error or ""))
