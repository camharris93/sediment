"""Grounding — dynamic schema discovery + synthetic-CTE inlining (the multi-hop /
multi-turn rewrite that turns a bare prior-result name into an inline CTE)."""
from __future__ import annotations

import sqlglot

from engine.query.grounding import (
    ColumnInfo,
    GroundingContext,
    TableInfo,
    inline_synthetic_ctes,
)


def test_grounding_discovers_tables(grounding):
    fqns = {t.fully_qualified for t in grounding.tables}
    assert "anage_marts.mart_longevity_by_class" in fqns
    assert "anage_staging.stg_anage" in fqns


def test_grounding_reports_columns(grounding):
    mart = next(t for t in grounding.tables
                if t.name == "mart_longevity_by_class")
    cols = {c.name for c in mart.columns}
    assert {"class", "n_species", "max_longevity"} <= cols


def test_grounding_derives_grain(grounding):
    stg = next(t for t in grounding.tables if t.name == "stg_anage")
    # hagrid is unique per row → single-column grain
    assert stg.grain == ["hagrid"]


def _ctx_with_synthetic(sql_for_hop1):
    syn = TableInfo(
        schema="", name="hop1", fully_qualified="hop1", row_count=3,
        columns=[ColumnInfo("class", "VARCHAR", True)],
        is_synthetic=True, synthetic_sql=sql_for_hop1)
    return GroundingContext(schemas=[], tables=[syn], relationships=[])


def test_inline_synthetic_noop_without_synthetics():
    ctx = GroundingContext(schemas=[], tables=[], relationships=[])
    sql = "SELECT * FROM anage_marts.m"
    assert inline_synthetic_ctes(sql, ctx) == sql


def test_inline_synthetic_wraps_reference_as_cte():
    ctx = _ctx_with_synthetic("SELECT class FROM anage_marts.mart_longevity_by_class")
    out = inline_synthetic_ctes("SELECT * FROM hop1", ctx)
    assert "WITH" in out.upper()
    assert "hop1 AS (" in out
    # the result must be parseable DuckDB
    assert sqlglot.parse_one(out, dialect="duckdb") is not None


def test_inline_synthetic_merges_into_existing_with():
    ctx = _ctx_with_synthetic("SELECT class FROM anage_marts.mart_longevity_by_class")
    out = inline_synthetic_ctes(
        "WITH other AS (SELECT 1) SELECT * FROM hop1, other", ctx)
    assert sqlglot.parse_one(out, dialect="duckdb") is not None
    assert "hop1 AS (" in out and "other AS (" in out
