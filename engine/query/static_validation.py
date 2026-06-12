"""L3 — Static validation. Parse the candidate SQL with sqlglot and INDEPENDENTLY
verify it against the grounded schema WITHOUT touching the warehouse.

This is the differentiator: the LLM does not get to self-report what it used.
Catches hallucinated columns/tables, unqualified or wrong-schema references,
unresolved aliases, and ambiguous unqualified columns. Failures return structured
violations the L3->L2 loop feeds back to the generator.

Ported from sql-engine; dialect switched to DuckDB and "fully qualified" relaxed
to `schema.table` (DuckDB has no project layer).
"""
from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from .grounding import ColumnInfo, GroundingContext

DIALECT = "duckdb"


class ViolationKind(str, Enum):
    parse_error = "parse_error"
    qualification = "qualification"
    table_not_found = "table_not_found"
    alias_not_found = "alias_not_found"
    column_not_found = "column_not_found"
    ambiguous_column = "ambiguous_column"


@dataclass
class Violation:
    kind: ViolationKind
    message: str
    offending_token: str | None = None
    suggested_fix: str | None = None
    location: str | None = None

    def to_feedback_line(self) -> str:
        bits = [f"[{self.kind.value}]"]
        if self.offending_token:
            bits.append(f"`{self.offending_token}`")
        bits.append("- " + self.message)
        if self.suggested_fix:
            bits.append(f"(suggested fix: {self.suggested_fix})")
        if self.location:
            bits.append(f"in {self.location}")
        return " ".join(bits)


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def to_feedback(self) -> str:
        if self.ok:
            return ""
        lines = ["Validation failures (MEASURED against the live schema; the schema is the source of truth):"]
        for v in self.violations:
            lines.append(f"  - {v.to_feedback_line()}")
        return "\n".join(lines)


@dataclass
class _SchemaIndex:
    tables: dict[str, dict[str, ColumnInfo]]
    all_table_fqns: list[str]
    # Synthetic prior-result tables, keyed by BARE name (no schema). Referenced
    # unqualified and rewritten to CTEs at execution — exempt from qualification.
    synthetic: dict[str, dict[str, ColumnInfo]]


def _build_index(ctx: GroundingContext) -> _SchemaIndex:
    tables: dict[str, dict[str, ColumnInfo]] = {}
    synthetic: dict[str, dict[str, ColumnInfo]] = {}
    for t in ctx.tables:
        cols = {c.name.lower(): c for c in t.columns}
        if t.is_synthetic:
            synthetic[t.name.lower()] = cols
        else:
            tables[t.fully_qualified.lower()] = cols
    return _SchemaIndex(tables=tables, all_table_fqns=sorted(tables.keys()), synthetic=synthetic)


def _table_fqn(node: exp.Table) -> str:
    """Render a Table node to lowercase `schema.name`. DuckDB tables are
    schema-qualified; a stray catalog prefix is folded down to the last two parts."""
    parts = [p for p in [
        (node.catalog or "").replace('"', "").lower(),
        (node.db or "").replace('"', "").lower(),
        (node.name or "").replace('"', "").lower(),
    ] if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "")


def _is_qualified(node: exp.Table) -> bool:
    # schema.table is enough for DuckDB (db + name); catalog is optional.
    return bool(node.db) and bool(node.name)


def _suggest_column(target: str, candidates: Iterable[str]) -> str | None:
    cands = list(candidates)
    matches = difflib.get_close_matches(target.lower(), [c.lower() for c in cands], n=1, cutoff=0.4)
    if matches:
        for c in cands:
            if c.lower() == matches[0]:
                return c
    for c in cands:
        cl = c.lower()
        if cl and (cl in target.lower() or target.lower() in cl):
            return c
    return None


def _suggest_table(target: str, candidates: Iterable[str]) -> str | None:
    cands = list(candidates)
    suffix = target.lower().split(".")[-1]
    hits = [c for c in cands if c.endswith("." + suffix) or c.endswith(suffix)]
    if hits:
        return hits[0]
    m = difflib.get_close_matches(target.lower(), cands, n=1, cutoff=0.5)
    return m[0] if m else None


def _scope_columns(scope: Scope, schema: _SchemaIndex):
    cols_by_source: dict[str, set[str]] = {}
    for alias, src in scope.sources.items():
        al = alias.lower()
        if isinstance(src, exp.Table):
            fq = _table_fqn(src)
            tbl = schema.tables.get(fq)
            if tbl is None and not src.db and not src.catalog:
                # Synthetic prior-result table, referenced by bare name.
                tbl = schema.synthetic.get((src.name or "").lower())
            cols_by_source[al] = set(tbl.keys()) if tbl else set()
        elif isinstance(src, Scope):
            cols_by_source[al] = {
                (p.alias_or_name or "").lower()
                for p in src.expression.expressions if p.alias_or_name
            }
        else:
            cols_by_source[al] = set()
    return cols_by_source, scope.sources


def _projection_aliases(scope: Scope) -> set[str]:
    out: set[str] = set()
    for proj in scope.expression.expressions:
        if isinstance(proj, exp.Alias) and proj.alias:
            out.add(proj.alias.lower())
    return out


def validate_sql(sql: str, ctx: GroundingContext) -> ValidationResult:
    schema = _build_index(ctx)
    violations: list[Violation] = []

    try:
        ast = sqlglot.parse_one(sql, dialect=DIALECT)
    except ParseError as e:
        return ValidationResult(False, [Violation(ViolationKind.parse_error,
                                f"sqlglot could not parse the SQL (DuckDB dialect): {e}")])
    if ast is None:
        return ValidationResult(False, [Violation(ViolationKind.parse_error, "empty AST")])

    scopes = list(traverse_scope(ast))
    cte_names: set[str] = set()
    for s in scopes:
        if s.is_cte:
            cte = s.expression.parent
            if isinstance(cte, exp.CTE) and cte.alias_or_name:
                cte_names.add(cte.alias_or_name.lower())

    # Table existence + qualification.
    for tbl in ast.find_all(exp.Table):
        name_l = (tbl.name or "").lower()
        if name_l in cte_names and not tbl.db:
            continue
        # Synthetic prior-result tables are referenced unqualified and inlined as
        # CTEs at execution — exempt from qualification + existence.
        if name_l in schema.synthetic and not tbl.db and not tbl.catalog:
            continue
        if not _is_qualified(tbl):
            violations.append(Violation(
                ViolationKind.qualification,
                "table reference is not schema-qualified — write it as `schema.table`",
                offending_token=(tbl.name or tbl.sql(dialect=DIALECT)),
            ))
            continue
        fq = _table_fqn(tbl)
        if fq not in schema.tables:
            violations.append(Violation(
                ViolationKind.table_not_found,
                f"table `{fq}` does not exist in the grounded schemas",
                offending_token=fq, suggested_fix=_suggest_table(fq, schema.all_table_fqns),
            ))

    # Column resolution per scope.
    for scope in scopes:
        cols_by_source, source_map = _scope_columns(scope, schema)
        own_aliases = _projection_aliases(scope)
        unq: dict[str, list[str]] = {}
        for src_alias, cols in cols_by_source.items():
            for c in cols:
                unq.setdefault(c, []).append(src_alias)

        for col in scope.columns:
            cname = (col.name or "").lower()
            tref = (col.table or "").lower()
            if tref:
                if tref not in cols_by_source:
                    # Maybe an outer-scope source.
                    outer = False
                    sp = scope.parent
                    while sp is not None:
                        oc, _ = _scope_columns(sp, schema)
                        if tref in oc:
                            outer = True
                            break
                        sp = sp.parent
                    if not outer:
                        violations.append(Violation(
                            ViolationKind.alias_not_found,
                            f"alias or table `{col.table}` is not in scope here",
                            offending_token=col.table, location="column reference"))
                        continue
                cols = cols_by_source.get(tref, set())
                if cols and cname not in cols:
                    src = source_map.get(tref)
                    label = _table_fqn(src) if isinstance(src, exp.Table) else tref
                    violations.append(Violation(
                        ViolationKind.column_not_found,
                        f"column `{col.name}` does not exist on `{label}`",
                        offending_token=col.name, suggested_fix=_suggest_column(cname, cols),
                        location=f"reference {col.table}.{col.name}"))
            else:
                if cname in own_aliases:
                    continue
                matches = unq.get(cname, [])
                if not matches:
                    outer = False
                    sp = scope.parent
                    while sp is not None:
                        oc, _ = _scope_columns(sp, schema)
                        if any(cname in v for v in oc.values()):
                            outer = True
                            break
                        sp = sp.parent
                    if outer:
                        continue
                    allc: list[str] = []
                    for cols in cols_by_source.values():
                        allc.extend(cols)
                    violations.append(Violation(
                        ViolationKind.column_not_found,
                        f"column `{col.name}` does not exist on any table in scope for this SELECT",
                        offending_token=col.name, suggested_fix=_suggest_column(cname, allc),
                        location="unqualified column reference"))
                elif len(matches) > 1:
                    violations.append(Violation(
                        ViolationKind.ambiguous_column,
                        f"column `{col.name}` is ambiguous — it exists on multiple in-scope tables "
                        f"({', '.join(sorted(matches))}); qualify it",
                        offending_token=col.name, location="unqualified column reference"))

    return ValidationResult(ok=not violations, violations=violations)
