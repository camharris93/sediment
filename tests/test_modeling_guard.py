"""Build-a-model governance: the view/build boundary is enforced SERVER-SIDE, and
chat SQL promoted into a model is re-checked as read-only before it ever runs on a
read-WRITE connection.
"""
from __future__ import annotations

import pytest

import engine.config as config
from engine.modeling import (
    BuildModeError,
    UnsafeModelSQLError,
    require_build_mode,
    require_read_only_select,
    to_dbt_refs,
    validate_model_name,
)


@pytest.fixture
def view_mode(monkeypatch):
    monkeypatch.setenv("SEDIMENT_MODE", "view")


@pytest.fixture
def build_mode(monkeypatch):
    monkeypatch.setenv("SEDIMENT_MODE", "build")


def test_view_mode_refuses_build(view_mode):
    with pytest.raises(BuildModeError):
        require_build_mode()


def test_build_mode_allows_build(build_mode):
    require_build_mode()  # must not raise


def test_app_mode_defaults_to_view(monkeypatch):
    monkeypatch.delenv("SEDIMENT_MODE", raising=False)
    assert config.app_mode() == "view"
    assert config.is_build_mode() is False


def test_unknown_mode_falls_back_to_view(monkeypatch):
    monkeypatch.setenv("SEDIMENT_MODE", "admin")
    assert config.app_mode() == "view"


# ── read-only re-check on promoted SQL ───────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE anage_marts.x",
    "SELECT * FROM read_csv('/etc/passwd')",
    "DELETE FROM anage_marts.x",
    "COPY (SELECT 1) TO '/tmp/x.csv'",
])
def test_unsafe_promoted_sql_is_refused(sql):
    with pytest.raises(UnsafeModelSQLError):
        require_read_only_select(sql)


def test_safe_promoted_sql_passes():
    require_read_only_select("SELECT class, n_species FROM anage_marts.mart_longevity_by_class")


# ── name validation + ref rewriting ──────────────────────────────────────────

def test_model_name_must_be_snake_case():
    with pytest.raises(ValueError):
        validate_model_name("Bad Name!")


def test_model_name_gets_mart_prefix():
    assert validate_model_name("lifespan_by_order") == "mart_lifespan_by_order"


def test_model_name_keeps_existing_prefix():
    assert validate_model_name("int_helper") == "int_helper"


def test_to_dbt_refs_rewrites_schemas():
    out = to_dbt_refs("SELECT * FROM anage_marts.foo JOIN anage_staging.bar USING (id)")
    assert "{{ ref('anage__foo') }}" in out
    assert "{{ ref('anage__bar') }}" in out


def test_to_dbt_refs_rewrites_raw_to_source():
    out = to_dbt_refs("SELECT * FROM anage_raw.orders")
    assert "{{ source('anage', 'orders') }}" in out
