"""L6 — Result plausibility. The "silently wrong" guard: checks the ANSWER, using
the SQL's AST to reason about expected magnitudes.

Headline check: JOIN FAN-OUT — a parent-grain column aggregated across a 1:many
join to a child without pre-aggregation. Plus value-sanity checks (empty result,
all-null scalar, out-of-range percents, negative counts). Ported from sql-engine,
dialect switched to DuckDB and fully-qualified names rendered as `schema.table`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from .grounding import GroundingContext, Relationship

DIALECT = "duckdb"


class WarningKind(str, Enum):
    fanout_risk = "fanout_risk"
    empty_result = "empty_result"
    all_null_aggregate = "all_null_aggregate"
    out_of_range = "out_of_range"
    negative_count = "negative_count"


@dataclass
class PlausibilityWarning:
    kind: WarningKind
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlausibilityResult:
    ok: bool
    warnings: list[PlausibilityWarning] = field(default_factory=list)


def _outermost_select(ast: exp.Expression) -> exp.Select | None:
    return ast if isinstance(ast, exp.Select) else ast.find(exp.Select)


def _table_fq(tbl: exp.Table) -> str | None:
    parts = [p for p in [(tbl.catalog or ""), (tbl.db or ""), (tbl.name or "")] if p]
    if len(parts) < 2:
        return None
    return ".".join(parts[-2:]).lower()


def _resolve_alias_map(ast: exp.Expression) -> dict[str, str]:
    out: dict[str, str] = {}
    outer = _outermost_select(ast)
    if outer is None:
        return out
    from_clause = outer.args.get("from")
    joins = outer.args.get("joins") or []
    tables: list[exp.Table] = []
    if from_clause is not None:
        first = from_clause.this if isinstance(from_clause, exp.From) else from_clause
        if isinstance(first, exp.Table):
            tables.append(first)
    for j in joins:
        rhs = j.this if isinstance(j, exp.Join) else None
        if isinstance(rhs, exp.Table):
            tables.append(rhs)
    for tbl in tables:
        fq = _table_fq(tbl)
        if not fq:
            continue
        key = (tbl.alias_or_name or "").lower()
        if key:
            out[key] = fq
    return out


def _columns_by_table(ctx: GroundingContext) -> dict[str, set[str]]:
    return {t.fully_qualified.lower(): {c.name.lower() for c in t.columns} for t in ctx.tables}


def _aggs_in_main_query(ast: exp.Expression) -> list[exp.AggFunc]:
    cte_ids: set[int] = set()
    with_node = ast.find(exp.With)
    if with_node is not None:
        for cte in with_node.expressions:
            if isinstance(cte, exp.CTE):
                cte_ids.add(id(cte))

    def in_cte(node: exp.Expression) -> bool:
        p = node.parent
        while p is not None:
            if id(p) in cte_ids:
                return True
            p = p.parent
        return False

    return [n for n in ast.find_all(exp.AggFunc) if not in_cte(n)]


def _resolve_col(col: exp.Column, alias_map: dict[str, str], cols_by_table: dict[str, set[str]]) -> str | None:
    if col.table:
        return alias_map.get(col.table.lower())
    name = (col.name or "").lower()
    matches = [fq for fq in set(alias_map.values()) if name in cols_by_table.get(fq, set())]
    return matches[0] if len(matches) == 1 else None


def check_fanout(sql: str, ctx: GroundingContext) -> list[PlausibilityWarning]:
    try:
        ast = sqlglot.parse_one(sql, dialect=DIALECT)
    except ParseError:
        return []
    if ast is None:
        return []
    alias_map = _resolve_alias_map(ast)
    if len(alias_map) < 2:
        return []
    in_query = set(alias_map.values())
    cols_by_table = _columns_by_table(ctx)
    pairs: list[Relationship] = [
        r for r in ctx.relationships
        if r.cardinality == "1:many" and r.parent.lower() in in_query and r.child.lower() in in_query
    ]
    if not pairs:
        return []

    warnings: list[PlausibilityWarning] = []
    seen: set[tuple] = set()
    for agg in _aggs_in_main_query(ast):
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Star):
            for rel in pairs:
                key = (rel.parent.lower(), rel.child.lower(), "COUNT(*)")
                if key in seen:
                    continue
                seen.add(key)
                warnings.append(PlausibilityWarning(
                    WarningKind.fanout_risk,
                    f"COUNT(*) over a join from `{rel.parent}` to its 1:many child "
                    f"`{rel.child}` counts child rows, not parent rows.",
                    {"parent": rel.parent, "child": rel.child, "aggregate": "COUNT(*)"}))
            continue
        for col in agg.find_all(exp.Column):
            fq = _resolve_col(col, alias_map, cols_by_table)
            if not fq:
                continue
            for rel in pairs:
                if fq != rel.parent.lower():
                    continue
                key = (rel.parent.lower(), rel.child.lower(), col.name.lower())
                if key in seen:
                    continue
                seen.add(key)
                warnings.append(PlausibilityWarning(
                    WarningKind.fanout_risk,
                    f"Aggregating `{col.name}` (on parent `{rel.parent}`) across a 1:many join "
                    f"to `{rel.child}` inflates the {type(agg).__name__.upper()} by the fan-out factor. "
                    "Pre-aggregate the child to parent grain first.",
                    {"parent": rel.parent, "child": rel.child, "column": col.name}))
                break
    return warnings


_PCT = [re.compile(rf"(^|_){re.escape(t)}($|_)", re.I) for t in ("pct", "percent", "rate", "ratio")]


def _is_pct(name: str) -> bool:
    return any(p.search(name) for p in _PCT)


def _is_count(name: str) -> bool:
    n = name.lower()
    return n.startswith(("count", "n_", "num_", "cnt_")) or n.endswith(("_count", "_n", "_num", "_cnt"))


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def check_result_values(rows: list[dict[str, Any]]) -> list[PlausibilityWarning]:
    warnings: list[PlausibilityWarning] = []
    if not rows:
        warnings.append(PlausibilityWarning(
            WarningKind.empty_result,
            "Executed without error but returned zero rows. If you expected >=1, the filter is "
            "likely too tight or the identifier doesn't match the data."))
        return warnings
    for k in rows[0].keys():
        vals = [r.get(k) for r in rows]
        non_null = [v for v in vals if v is not None]
        if len(rows) == 1 and not non_null:
            warnings.append(PlausibilityWarning(
                WarningKind.all_null_aggregate,
                f"The single-row aggregate column `{k}` is NULL.", {"column": k}))
            continue
        nums = [v for v in non_null if _num(v)]
        if _is_pct(k) and nums:
            lo, hi = min(nums), max(nums)
            if lo < 0 or hi > 100:
                warnings.append(PlausibilityWarning(
                    WarningKind.out_of_range,
                    f"Column `{k}` looks like a percentage/rate but ranges [{lo}, {hi}], "
                    "outside any plausible [0,1] or [0,100] band.", {"column": k, "min": lo, "max": hi}))
        if _is_count(k) and any(v < 0 for v in nums):
            warnings.append(PlausibilityWarning(
                WarningKind.negative_count,
                f"Column `{k}` looks like a count but has negative values.", {"column": k}))
    return warnings


def check_plausibility(sql: str, rows: list[dict[str, Any]], ctx: GroundingContext) -> PlausibilityResult:
    warnings = check_fanout(sql, ctx) + check_result_values(rows)
    return PlausibilityResult(ok=not warnings, warnings=warnings)
