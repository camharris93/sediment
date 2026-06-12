"""L3 static validation — independently verify generated SQL against the grounded
schema WITHOUT touching the warehouse. This is what stops a hallucinated column or
table from ever reaching execution.
"""
from __future__ import annotations

from engine.query.static_validation import ViolationKind, validate_sql


def test_valid_sql_passes(grounding):
    r = validate_sql(
        "SELECT class, n_species FROM anage_marts.mart_longevity_by_class", grounding)
    assert r.ok, [v.message for v in r.violations]


def test_valid_join_passes(grounding):
    r = validate_sql(
        "SELECT s.species, m.n_species FROM anage_staging.stg_anage s "
        "JOIN anage_marts.mart_longevity_by_class m ON s.class = m.class", grounding)
    assert r.ok, [v.message for v in r.violations]


def test_hallucinated_column_is_caught(grounding):
    r = validate_sql(
        "SELECT nonexistent_col FROM anage_marts.mart_longevity_by_class", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.column_not_found for v in r.violations)


def test_hallucinated_table_is_caught(grounding):
    r = validate_sql("SELECT * FROM anage_marts.mart_does_not_exist", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.table_not_found for v in r.violations)


def test_unqualified_table_is_caught(grounding):
    # The agent is required to write schema.table; a bare table name is rejected.
    r = validate_sql("SELECT * FROM mart_longevity_by_class", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.qualification for v in r.violations)


def test_bad_alias_is_caught(grounding):
    r = validate_sql(
        "SELECT x.class FROM anage_marts.mart_longevity_by_class m", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.alias_not_found for v in r.violations)


def test_column_on_wrong_table_is_caught(grounding):
    # body_mass_g exists on stg_anage, not on the mart.
    r = validate_sql(
        "SELECT m.body_mass_g FROM anage_marts.mart_longevity_by_class m", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.column_not_found for v in r.violations)


def test_parse_error_is_structured(grounding):
    r = validate_sql("SELECT FROM WHERE )(", grounding)
    assert not r.ok
    assert any(v.kind == ViolationKind.parse_error for v in r.violations)


def test_suggested_fix_points_at_real_column(grounding):
    r = validate_sql(
        "SELECT n_specis FROM anage_marts.mart_longevity_by_class", grounding)  # typo
    assert not r.ok
    cols = [v.suggested_fix for v in r.violations if v.suggested_fix]
    assert "n_species" in cols
