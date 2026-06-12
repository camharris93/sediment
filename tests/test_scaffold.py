"""Deterministic scaffold baseline — the no-key tier. Must produce a clean,
snake_cased, conservatively-tested staging model from a profile alone."""
from __future__ import annotations

from engine.scaffold import _dedupe, deterministic_staging, snake_case


def test_snake_case_basics():
    assert snake_case("Body mass (g)") == "body_mass_g"
    assert snake_case("Max longevity (yrs)") == "max_longevity_yrs"
    assert snake_case("HAGRID") == "hagrid"
    assert snake_case("weird/name-here") == "weird_name_here"


def test_snake_case_leading_digit_and_reserved():
    assert snake_case("123abc").startswith("c_")
    assert snake_case("order") == "order_"   # reserved word gets suffixed
    assert snake_case("") == "col"           # never empty


def test_dedupe_collisions():
    assert _dedupe(["a", "a", "a", "b"]) == ["a", "a_1", "a_2", "b"]


def _profile():
    return {
        "table": "anage",
        "columns": [
            {"name": "HAGRID", "type": "BIGINT", "is_candidate_key": True, "null_rate": 0.0},
            {"name": "Common name", "type": "VARCHAR", "is_candidate_key": False, "null_rate": 0.0},
            {"name": "Body mass (g)", "type": "DOUBLE", "is_candidate_key": False, "null_rate": 0.3},
        ],
    }


def test_staging_sql_structure():
    sql, _ = deterministic_staging(_profile(), "anage")
    assert "{{ config(schema='anage_staging', alias='stg_anage') }}" in sql
    assert "{{ source('anage', 'anage') }}" in sql
    assert '"HAGRID" ' in sql and "as hagrid" in sql
    assert "as body_mass_g" in sql
    # last projected column has no trailing comma before `from source`
    assert ",\n    from source" not in sql


def test_schema_yml_tests_are_conservative():
    _, yml = deterministic_staging(_profile(), "anage")
    # candidate key -> not_null + unique
    assert "not_null" in yml and "unique" in yml
    # the non-null name column gets not_null; the 30%-null body mass gets NO test
    assert "body_mass_g" in yml
    # unique should appear exactly once (only the key column), proving we don't
    # over-test the non-key columns
    assert yml.count("- unique") == 1


def test_model_name_is_dataset_prefixed():
    _, yml = deterministic_staging(_profile(), "anage")
    assert "name: anage__stg_anage" in yml
