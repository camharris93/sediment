"""L0 — Grounding & profiling over the local DuckDB warehouse.

Builds a GroundingContext dynamically from `information_schema` + cheap local
aggregates, so the agent adapts to WHATEVER marts exist for any dataset (PRD
§4.1 "schema pulled dynamically from DuckDB"). Grain and join cardinality are
DERIVED by measurement, never hinted — same principle as the sql-engine original,
minus the BigQuery cost machinery (everything here is a local file scan).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from .warehouse import DuckDBWarehouse

# Schemas the query agent is allowed to read. Marts are the curated answer
# surface; staging is the clean per-row contract that powers detailed questions.
GROUNDED_SCHEMAS = ["marts", "staging"]

_KEY_NAME_RE = re.compile(r"(^|_)(id|key|code|hagrid|rank|name)($|_)|_id$|^id$", re.IGNORECASE)
_NUMERIC = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE",
            "DECIMAL", "REAL", "UINTEGER", "UBIGINT"}
_TEMPORAL = {"DATE", "TIMESTAMP", "TIME"}
_SAMPLEABLE = {"VARCHAR", "BOOLEAN", "INTEGER", "BIGINT"}
_SAMPLE_TOPK = 8


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    approx_distinct: int | None = None
    null_rate: float | None = None
    min_value: Any = None
    max_value: Any = None
    sample_values: list[Any] = field(default_factory=list)
    exact_distinct: int | None = None


@dataclass
class TableInfo:
    schema: str
    name: str
    fully_qualified: str          # schema.name
    row_count: int
    columns: list[ColumnInfo]
    grain: list[str] | None = None
    grain_method: str = "none"


@dataclass
class Relationship:
    parent: str
    child: str
    columns: list[str]
    cardinality: str              # "1:1" | "1:many" | "many:many"


@dataclass
class GroundingContext:
    schemas: list[str]
    tables: list[TableInfo]
    relationships: list[Relationship]
    built_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _base_type(t: str) -> str:
    return t.split("(")[0].strip().upper()


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _looks_like_key(name: str) -> bool:
    return bool(_KEY_NAME_RE.search(name))


# ─────────────────────────────────────────────────────────────────────────────

def _profile_column(con, fq: str, col: ColumnInfo, row_count: int) -> None:
    q = _q(col.name)
    bt = _base_type(col.type)
    null_count, approx = con.execute(
        f"SELECT COUNT(*) - COUNT({q}), approx_count_distinct({q}) FROM {fq}"
    ).fetchone()
    col.null_rate = (int(null_count) / row_count) if row_count else 0.0
    col.approx_distinct = int(approx or 0)
    # Exact distinct for key-shaped, non-null columns (cheap locally) → grain.
    if row_count and null_count == 0 and (_looks_like_key(col.name) or col.approx_distinct >= 0.9 * row_count):
        (exact,) = con.execute(f"SELECT COUNT(DISTINCT {q}) FROM {fq}").fetchone()
        col.exact_distinct = int(exact)
    if bt in _NUMERIC or bt in _TEMPORAL:
        col.min_value, col.max_value = con.execute(
            f"SELECT MIN({q}), MAX({q}) FROM {fq}"
        ).fetchone()
    if bt in _SAMPLEABLE and col.approx_distinct <= 2000:
        rows = con.execute(
            f"SELECT {q} FROM {fq} WHERE {q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT {_SAMPLE_TOPK}"
        ).fetchall()
        col.sample_values = [r[0] for r in rows]


def _derive_grain(table: TableInfo) -> None:
    singles = [c.name for c in table.columns
               if c.exact_distinct is not None and c.exact_distinct == table.row_count]
    if singles:
        singles.sort(key=lambda s: (len(s), s))
        table.grain = [singles[0]]
        table.grain_method = "single_column"


def _infer_relationships(tables: list[TableInfo]) -> list[Relationship]:
    rels: list[Relationship] = []
    col_index: dict[str, list[tuple[TableInfo, ColumnInfo, bool]]] = {}
    for t in tables:
        for c in t.columns:
            if not _looks_like_key(c.name):
                continue
            in_grain = bool(t.grain and t.grain == [c.name])
            col_index.setdefault(c.name.lower(), []).append((t, c, in_grain))

    seen: set[tuple[str, str, str]] = set()
    for col_name, entries in col_index.items():
        if len(entries) < 2:
            continue
        for (ta, ca, ua), (tb, cb, ub) in combinations(entries, 2):
            if _base_type(ca.type) != _base_type(cb.type):
                continue
            key = tuple(sorted([ta.fully_qualified, tb.fully_qualified]) + [col_name])
            if key in seen:
                continue
            seen.add(key)
            if ua and ub:
                rels.append(Relationship(ta.fully_qualified, tb.fully_qualified, [ca.name], "1:1"))
            elif ua and not ub:
                rels.append(Relationship(ta.fully_qualified, tb.fully_qualified, [ca.name], "1:many"))
            elif ub and not ua:
                rels.append(Relationship(tb.fully_qualified, ta.fully_qualified, [ca.name], "1:many"))
            else:
                p, c_ = sorted([ta.fully_qualified, tb.fully_qualified])
                rels.append(Relationship(p, c_, [ca.name], "many:many"))
    return rels


def build_grounding_context(schemas: list[str] | None = None) -> GroundingContext:
    schemas = schemas or GROUNDED_SCHEMAS
    wh = DuckDBWarehouse()
    con = wh._connect()
    tables: list[TableInfo] = []
    try:
        placeholders = ",".join("?" for _ in schemas)
        rows = con.execute(
            f"SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_schema IN ({placeholders}) ORDER BY table_schema, table_name",
            schemas,
        ).fetchall()
        for sch, tbl in rows:
            fq = f"{_q(sch)}.{_q(tbl)}"
            (rc,) = con.execute(f"SELECT COUNT(*) FROM {fq}").fetchone()
            cols_raw = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                [sch, tbl],
            ).fetchall()
            cols = [ColumnInfo(name=c[0], type=c[1], nullable=(str(c[2]).upper() == "YES"))
                    for c in cols_raw]
            t = TableInfo(schema=sch, name=tbl, fully_qualified=f"{sch}.{tbl}",
                          row_count=int(rc), columns=cols)
            for c in cols:
                try:
                    _profile_column(con, fq, c, int(rc))
                except Exception:
                    pass
            _derive_grain(t)
            tables.append(t)
    finally:
        con.close()
    rels = _infer_relationships(tables)
    return GroundingContext(schemas=schemas, tables=tables, relationships=rels)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt summary (consumed by L1 + L2)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, max_len: int = 40) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= max_len else s[: max_len - 1] + "..."


def to_prompt_summary(ctx: GroundingContext) -> str:
    lines = [
        "# Warehouse: a local DuckDB file.",
        f"# Schemas in scope: {', '.join(ctx.schemas)}",
        "",
        "All generated SQL MUST reference tables as `schema.table` (e.g. "
        "`marts.mart_longevity_by_class`). Double-quote identifiers that need it.",
        "",
        "## Tables",
    ]
    for t in ctx.tables:
        grain = ",".join(t.grain) if t.grain else "(no unique key derived)"
        lines.append("")
        lines.append(f"### `{t.fully_qualified}`  ({t.row_count:,} rows; grain = {grain})")
        for c in t.columns:
            extras = []
            if c.null_rate is not None:
                extras.append(f"null_rate={c.null_rate:.0%}")
            if c.exact_distinct is not None:
                extras.append(f"distinct={c.exact_distinct:,}")
            elif c.approx_distinct is not None:
                extras.append(f"~distinct={c.approx_distinct:,}")
            if c.min_value is not None or c.max_value is not None:
                extras.append(f"range=[{_fmt(c.min_value)} .. {_fmt(c.max_value)}]")
            if c.sample_values:
                rendered = ", ".join(repr(v) for v in c.sample_values[:5])
                extras.append(f"samples=[{rendered}]")
            extra_str = f"  ({'; '.join(extras)})" if extras else ""
            nn = "" if c.nullable else " NOT NULL"
            lines.append(f"  - `{c.name}` {c.type}{nn}{extra_str}")
    lines.append("")
    lines.append("## Derived relationships (MEASURED, not hinted)")
    if not ctx.relationships:
        lines.append("(none)")
    else:
        for r in ctx.relationships:
            lines.append(f"  - `{r.parent}` -> `{r.child}` on {', '.join(r.columns)} [{r.cardinality}]")
    lines.append("")
    lines.append(
        "**Cardinality rule:** joining a `1:many` parent to its child fans out the "
        "parent's rows. Pre-aggregate the child to the parent's grain before "
        "aggregating parent columns, or you will inflate SUM/AVG/COUNT."
    )
    return "\n".join(lines)
